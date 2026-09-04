#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx==0.28.1",
#   "rich==15.0.0",
#   "ruamel.yaml==0.19.1",
#   "semver==3.0.4",
#   "typer==0.27.1",
# ]
# ///
"""Pin, report, update, and semantically upgrade GitHub Actions by commit SHA."""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from io import StringIO
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import semver
import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import ScalarString
from ruamel.yaml.util import load_yaml_guess_indent

SHA = re.compile(r"^[0-9a-f]{40}$")
PARTIAL_VERSION = re.compile(
    r"^v(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"$"
)
YAML_SUFFIXES = {".yml", ".yaml"}

console = Console()
error_console = Console(stderr=True)
app = typer.Typer(
    add_completion=False,
    help="Pin GitHub Actions to immutable SHAs and manage semantic-version upgrades.",
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
)


class PinError(RuntimeError):
    """Expected operational error that should be shown without a traceback."""


class UpgradeLevel(StrEnum):
    """Semantic-version boundary allowed by an upgrade operation."""

    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


@dataclass(frozen=True, slots=True)
class SemanticRef:
    """Parsed semantic v-tag together with how many components were explicit."""

    text: str
    version: semver.Version
    precision: int


@dataclass(frozen=True, slots=True)
class RepositoryTag:
    """Semantic Git tag advertised by a GitHub repository."""

    name: str
    version: semver.Version
    precision: int
    commit: str


@dataclass(frozen=True, slots=True)
class UpgradeDecision:
    """Concrete current version and target tag selected for an upgrade."""

    current: RepositoryTag | None
    current_version: semver.Version
    target: RepositoryTag


@dataclass(frozen=True, slots=True)
class ActionChange:
    """Structured description of one planned or applied action-reference change."""

    path: Path
    line: int | None
    action: str
    old_ref: str
    new_ref: str
    old_commit: str | None
    new_commit: str
    old_version: str | None = None
    new_version: str | None = None


@dataclass(frozen=True, slots=True)
class FileEdit:
    """Complete in-memory rewrite plan for one YAML file."""

    path: Path
    before: str
    after: str
    changes: tuple[ActionChange, ...]

    @property
    def replacements(self) -> int:
        """Return the number of action references changed in this file."""
        return len(self.changes)


@dataclass(frozen=True, slots=True)
class VersionReport:
    """Available semantic-version updates for one action reference."""

    path: Path
    line: int | None
    action: str
    source_ref: str | None
    current: RepositoryTag | None
    current_version: semver.Version | None
    latest_patch: RepositoryTag | None
    latest_minor: RepositoryTag | None
    latest_major: RepositoryTag | None
    note: str | None = None


def repository(action: str) -> tuple[str, str]:
    """Extract the owning GitHub repository from an action or reusable-workflow path."""
    parts = action.split("/")
    if len(parts) < 2:
        raise PinError(f"Cannot determine GitHub repository for {action!r}")
    return parts[0], parts[1]


def semantic_ref(ref: str) -> SemanticRef | None:
    """Parse v1, v1.2, or a full SemVer v1.2.3[-pre][+build] tag."""
    if not ref.startswith("v"):
        return None

    partial = PARTIAL_VERSION.fullmatch(ref)
    if partial:
        components = [
            partial.group("major"),
            partial.group("minor"),
            partial.group("patch"),
        ]
        precision = next(
            (index for index, value in enumerate(components) if value is None),
            3,
        )
        numbers = [int(value) if value is not None else 0 for value in components]
        return SemanticRef(ref, semver.Version(*numbers), precision)

    try:
        version = semver.Version.parse(ref[1:])
    except ValueError:
        return None
    return SemanticRef(ref, version, 3)


def version_scope_contains(scope: SemanticRef, candidate: RepositoryTag) -> bool:
    """Return whether a concrete tag belongs to a broad ref such as v4 or v4.2."""
    if scope.precision >= 1 and candidate.version.major != scope.version.major:
        return False
    if scope.precision >= 2 and candidate.version.minor != scope.version.minor:
        return False
    if scope.precision >= 3:
        return candidate.version == scope.version
    return True


def upgrade_scope_contains(
    current: semver.Version,
    candidate: RepositoryTag,
    level: UpgradeLevel,
) -> bool:
    """Return whether candidate stays inside the requested SemVer upgrade boundary."""
    if level == UpgradeLevel.PATCH:
        return (
            candidate.version.major == current.major
            and candidate.version.minor == current.minor
        )
    if level == UpgradeLevel.MINOR:
        return candidate.version.major == current.major
    return True


class GitHub:
    """Small GitHub REST client specialized for action ref and tag resolution."""

    def __init__(self, client: httpx.Client) -> None:
        """Store the configured HTTP client used for all GitHub requests."""
        self.client = client

    def _get(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        """GET one GitHub endpoint while converting transport failures to PinError."""
        try:
            response = self.client.get(endpoint, **kwargs)
        except httpx.HTTPError as exc:
            raise PinError(f"GitHub request failed: {exc}") from None

        # GitHub Actions does not follow renamed-action repository redirects, so
        # silently accepting an API redirect could pin something the workflow
        # itself will not execute under the old repository name.
        if response.is_redirect:
            raise PinError(
                f"GitHub redirected {response.request.url}; "
                "update the repository name explicitly"
            )
        return response

    @staticmethod
    def _error(response: httpx.Response, context: str) -> PinError:
        """Build a concise GitHub API error with the server's message when available."""
        try:
            detail = response.json().get("message")
        except (ValueError, AttributeError):
            detail = None
        return PinError(
            f"GitHub could not {context}: "
            f"HTTP {response.status_code} {detail or response.reason_phrase}"
        )

    @cache
    def resolve(self, action: str, ref: str) -> str:
        """Resolve an action branch or tag to the full commit GitHub will execute."""
        owner, repo = repository(action)
        endpoint = (
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/"
            f"commits/{quote(ref, safe='')}"
        )
        response = self._get(endpoint)
        if response.status_code >= 400:
            raise self._error(response, f"resolve {action}@{ref}")

        try:
            commit = response.json()["sha"]
        except (ValueError, KeyError, TypeError):
            raise PinError(
                f"GitHub returned an invalid response for {action}@{ref}"
            ) from None

        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise PinError(
                f"GitHub returned an invalid commit for {action}@{ref}: {commit!r}"
            )
        return commit

    @cache
    def tags(self, action: str) -> tuple[RepositoryTag, ...]:
        """Return all semantic v-tags advertised for an action repository."""
        owner, repo = repository(action)
        endpoint = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/tags"
        tags: list[RepositoryTag] = []

        # GitHub caps this endpoint at 100 items per page. A 10,000-tag ceiling
        # protects an accidental or hostile repository from causing unbounded work.
        for page in range(1, 101):
            response = self._get(endpoint, params={"per_page": 100, "page": page})
            if response.status_code >= 400:
                raise self._error(response, f"list tags for {owner}/{repo}")

            try:
                payload = response.json()
            except ValueError:
                raise PinError(
                    f"GitHub returned invalid tags for {owner}/{repo}"
                ) from None
            if not isinstance(payload, list):
                raise PinError(f"GitHub returned invalid tags for {owner}/{repo}")

            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                commit = item.get("commit", {}).get("sha")
                parsed = semantic_ref(name) if isinstance(name, str) else None
                if parsed and isinstance(commit, str) and SHA.fullmatch(commit):
                    tags.append(
                        RepositoryTag(name, parsed.version, parsed.precision, commit)
                    )

            if len(payload) < 100:
                return tuple(tags)

        raise PinError(f"Refusing to scan more than 10,000 tags for {owner}/{repo}")

    def current_semantic_tag(
        self,
        action: str,
        source_ref: str,
        current_commit: str,
        *,
        include_prereleases: bool,
    ) -> RepositoryTag | None:
        """Find the concrete semantic tag represented by a broad ref and commit."""
        source = semantic_ref(source_ref)
        if source is None:
            return None

        matches = [
            tag
            for tag in self.tags(action)
            if tag.commit == current_commit
            and version_scope_contains(source, tag)
            and (include_prereleases or tag.version.prerelease is None)
        ]
        if not matches:
            return None
        return max(matches, key=lambda tag: (tag.version, tag.precision, tag.name))

    @staticmethod
    def newest_tag(
        tags: list[RepositoryTag],
        *,
        current: semver.Version,
        level: UpgradeLevel,
    ) -> RepositoryTag | None:
        """Return the newest tag newer than current within an upgrade boundary."""
        candidates = [
            tag
            for tag in tags
            if tag.version > current and upgrade_scope_contains(current, tag, level)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda tag: (tag.version, tag.precision, tag.name))

    def version_report(
        self,
        action: str,
        source_ref: str,
        current_commit: str,
        *,
        include_prereleases: bool,
    ) -> tuple[
        RepositoryTag | None,
        semver.Version,
        RepositoryTag | None,
        RepositoryTag | None,
        RepositoryTag | None,
    ]:
        """Return current and newest patch/minor/major tags for one action."""
        source = semantic_ref(source_ref)
        if source is None:
            raise PinError(
                f"Cannot report versions for non-semantic ref {action}@{source_ref}"
            )

        tags = list(self.tags(action))
        if not include_prereleases:
            tags = [tag for tag in tags if tag.version.prerelease is None]

        current_tag = self.current_semantic_tag(
            action,
            source_ref,
            current_commit,
            include_prereleases=include_prereleases,
        )
        current = current_tag.version if current_tag else source.version

        return (
            current_tag,
            current,
            self.newest_tag(tags, current=current, level=UpgradeLevel.PATCH),
            self.newest_tag(tags, current=current, level=UpgradeLevel.MINOR),
            self.newest_tag(tags, current=current, level=UpgradeLevel.MAJOR),
        )

    def choose_upgrade(
        self,
        action: str,
        source_ref: str,
        current_commit: str,
        level: UpgradeLevel,
        *,
        include_prereleases: bool,
    ) -> UpgradeDecision:
        """Choose the newest semantic tag allowed by the requested upgrade level."""
        source = semantic_ref(source_ref)
        if source is None:
            raise PinError(
                f"Cannot {level.value} upgrade non-semantic ref {action}@{source_ref}"
            )

        tags = list(self.tags(action))
        if not include_prereleases:
            tags = [tag for tag in tags if tag.version.prerelease is None]

        # A broad alias such as v4 or v4.2 often points at a concrete v4.x.y tag.
        # Recover the concrete installed version from the SHA so patch/minor
        # selection starts from reality rather than assuming v4 means v4.0.0.
        current_tag = self.current_semantic_tag(
            action,
            source_ref,
            current_commit,
            include_prereleases=include_prereleases,
        )
        current = current_tag.version if current_tag else source.version

        if level == UpgradeLevel.PATCH and source.precision < 2 and current_tag is None:
            raise PinError(
                f"Cannot determine the current minor version for {action}@{source_ref}; "
                "use --upgrade minor or annotate the pin with vMAJOR.MINOR"
            )

        candidates = [
            tag for tag in tags if upgrade_scope_contains(current, tag, level)
        ]
        if not candidates:
            raise PinError(f"No semantic version tags found for {action}")

        target = max(candidates, key=lambda tag: (tag.version, tag.precision, tag.name))
        if target.version < current:
            # This can only happen when the annotated version is newer than the
            # repository's visible semantic tags. Never turn an upgrade into a downgrade.
            raise PinError(
                f"Newest eligible tag for {action} is {target.name}, "
                f"older than current {source_ref}"
            )

        return UpgradeDecision(current_tag, current, target)


def parse_uses(value: str) -> tuple[str, str] | None:
    """Parse an external GitHub `uses:` value into action path and ref."""
    if value.startswith(("./", "docker://", "${{")):
        return None

    action, separator, ref = value.rpartition("@")
    if not separator or not ref or len(action.split("/")) < 2:
        return None
    return action, ref


def comment(mapping: CommentedMap) -> str | None:
    """Return the end-of-line comment attached to a mapping's `uses` key."""
    parts = mapping.ca.items.get("uses")
    if not parts or len(parts) < 3 or parts[2] is None:
        return None
    return parts[2].value.strip().removeprefix("#").strip() or None


def annotated_ref(mapping: CommentedMap) -> str | None:
    """Extract the source ref stored at the start of this tool's pin comment."""
    text = comment(mapping)
    if not text:
        return None

    # Pins emitted by this script use '# v4' or '# v4; existing comment'. There
    # is intentionally no heavier marker: this keeps generated workflow YAML tidy.
    ref = text.partition(";")[0].strip()
    return ref if ref and not any(char.isspace() for char in ref) else None


def annotate(mapping: CommentedMap, ref: str) -> None:
    """Add a source-ref annotation while retaining any pre-existing comment."""
    existing = comment(mapping)
    if not existing:
        text = ref
    elif existing == ref or existing.startswith(f"{ref};"):
        text = existing
    else:
        text = f"{ref}; {existing}"
    mapping.yaml_add_eol_comment(text, key="uses")


def replace_annotation(mapping: CommentedMap, old_ref: str, new_ref: str) -> None:
    """Replace this tool's source-ref annotation and retain unrelated comment text."""
    existing = comment(mapping)
    if not existing:
        mapping.yaml_add_eol_comment(new_ref, key="uses")
        return

    head, separator, tail = existing.partition(";")
    if head.strip() != old_ref:
        annotate(mapping, new_ref)
        return

    text = new_ref if not separator else f"{new_ref};{tail}"
    mapping.yaml_add_eol_comment(text, key="uses")


def replace_scalar(original: str, replacement: str) -> str:
    """Replace a YAML scalar while preserving any quote-style scalar subclass."""
    return type(original)(replacement) if isinstance(original, ScalarString) else replacement


def uses_line(mapping: CommentedMap) -> int | None:
    """Return the one-based source line containing a mapping's `uses` key."""
    try:
        return mapping.lc.key("uses")[0] + 1
    except (AttributeError, KeyError, TypeError):
        return None


def pin_node(
    node: Any,
    path: Path,
    github: GitHub,
    *,
    update: bool,
    upgrade: UpgradeLevel | None,
    include_prereleases: bool,
) -> list[ActionChange]:
    """Recursively pin or upgrade all external `uses` entries under one YAML node."""
    changes: list[ActionChange] = []

    if isinstance(node, CommentedMap):
        value = node.get("uses")
        parsed = parse_uses(value) if isinstance(value, str) else None

        if parsed:
            action, current_ref = parsed
            pinned = bool(SHA.fullmatch(current_ref))
            source_ref = annotated_ref(node) if pinned else current_ref
            target_ref: str | None = None
            annotation_ref: str | None = None
            old_version: str | None = None
            new_version: str | None = None

            if upgrade and source_ref and semantic_ref(source_ref):
                current_commit = current_ref if pinned else github.resolve(action, source_ref)
                decision = github.choose_upgrade(
                    action,
                    source_ref,
                    current_commit,
                    upgrade,
                    include_prereleases=include_prereleases,
                )
                target_ref = decision.target.name
                annotation_ref = decision.target.name
                old_version = (
                    decision.current.name
                    if decision.current is not None
                    else source_ref
                )
                new_version = decision.target.name
            elif pinned:
                # Existing immutable pins stay fixed unless --update was requested.
                target_ref = source_ref if update else None
                annotation_ref = source_ref
            else:
                # Initial pinning always resolves the floating ref and records it.
                target_ref = source_ref
                annotation_ref = source_ref

            if target_ref:
                commit = github.resolve(action, target_ref)
                replacement = f"{action}@{commit}"
                changed_value = replacement != value
                changed_comment = bool(
                    annotation_ref
                    and upgrade
                    and annotation_ref != annotated_ref(node)
                )

                if changed_value:
                    node["uses"] = replace_scalar(value, replacement)
                if annotation_ref:
                    if upgrade:
                        replace_annotation(node, source_ref or "", annotation_ref)
                    elif not pinned:
                        annotate(node, annotation_ref)

                if changed_value or changed_comment:
                    changes.append(
                        ActionChange(
                            path=path,
                            line=uses_line(node),
                            action=action,
                            old_ref=current_ref,
                            new_ref=annotation_ref or target_ref,
                            old_commit=current_ref if pinned else None,
                            new_commit=commit,
                            old_version=old_version,
                            new_version=new_version,
                        )
                    )

        for child in node.values():
            changes.extend(
                pin_node(
                    child,
                    path,
                    github,
                    update=update,
                    upgrade=upgrade,
                    include_prereleases=include_prereleases,
                )
            )

    elif isinstance(node, CommentedSeq):
        for child in node:
            changes.extend(
                pin_node(
                    child,
                    path,
                    github,
                    update=update,
                    upgrade=upgrade,
                    include_prereleases=include_prereleases,
                )
            )

    return changes


def find_unpinned(node: Any) -> list[str]:
    """Recursively return external `uses` values that are not full SHA pins."""
    findings: list[str] = []

    if isinstance(node, CommentedMap):
        value = node.get("uses")
        parsed = parse_uses(value) if isinstance(value, str) else None
        if parsed and not SHA.fullmatch(parsed[1]):
            findings.append(value)

        for child in node.values():
            findings.extend(find_unpinned(child))

    elif isinstance(node, CommentedSeq):
        for child in node:
            findings.extend(find_unpinned(child))

    return findings


def collect_version_reports(
    node: Any,
    path: Path,
    github: GitHub,
    *,
    include_prereleases: bool,
) -> list[VersionReport]:
    """Recursively collect semantic-version freshness information for `uses` entries."""
    reports: list[VersionReport] = []

    if isinstance(node, CommentedMap):
        value = node.get("uses")
        parsed = parse_uses(value) if isinstance(value, str) else None
        if parsed:
            action, current_ref = parsed
            pinned = bool(SHA.fullmatch(current_ref))
            source_ref = annotated_ref(node) if pinned else current_ref
            source = semantic_ref(source_ref) if source_ref else None

            if source is None:
                reports.append(
                    VersionReport(
                        path=path,
                        line=uses_line(node),
                        action=action,
                        source_ref=source_ref,
                        current=None,
                        current_version=None,
                        latest_patch=None,
                        latest_minor=None,
                        latest_major=None,
                        note=(
                            "pinned SHA has no semantic version annotation"
                            if pinned
                            else f"non-semantic ref {current_ref!r}"
                        ),
                    )
                )
            else:
                current_commit = current_ref if pinned else github.resolve(action, current_ref)
                current_tag, current_version, patch, minor, major = github.version_report(
                    action,
                    source_ref,
                    current_commit,
                    include_prereleases=include_prereleases,
                )
                reports.append(
                    VersionReport(
                        path=path,
                        line=uses_line(node),
                        action=action,
                        source_ref=source_ref,
                        current=current_tag,
                        current_version=current_version,
                        latest_patch=patch,
                        latest_minor=minor,
                        latest_major=major,
                    )
                )

        for child in node.values():
            reports.extend(
                collect_version_reports(
                    child,
                    path,
                    github,
                    include_prereleases=include_prereleases,
                )
            )

    elif isinstance(node, CommentedSeq):
        for child in node:
            reports.extend(
                collect_version_reports(
                    child,
                    path,
                    github,
                    include_prereleases=include_prereleases,
                )
            )

    return reports


def guess_mapping_indent(source: str) -> int:
    """Infer indentation used for nested mapping keys in an existing YAML file."""
    levels = sorted(
        {
            len(line) - len(line.lstrip(" "))
            for line in source.splitlines()
            if line.strip()
            and not line.lstrip().startswith(("#", "-"))
            and line.rstrip().endswith(":")
        }
    )
    differences = [
        later - earlier
        for earlier, later in zip(levels, levels[1:])
        if later > earlier
    ]
    return min(differences, default=2)


def load_document(path: Path) -> tuple[str, Any, YAML]:
    """Load YAML in ruamel round-trip mode while preserving local formatting choices."""
    with path.open(encoding="utf-8", newline="") as file:
        source = file.read()

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 10_000
    yaml.line_break = "\r\n" if "\r\n" in source else "\n"

    data, sequence_indent, sequence_offset = load_yaml_guess_indent(source, yaml=yaml)
    mapping_indent = guess_mapping_indent(source)
    if sequence_indent is not None:
        yaml.indent(
            mapping=mapping_indent,
            sequence=sequence_indent,
            offset=sequence_offset or 0,
        )
    else:
        yaml.indent(mapping=mapping_indent)

    return source, data, yaml


def dump_document(source: str, data: Any, yaml: YAML) -> str:
    """Serialize a round-trip YAML document while preserving final-newline behavior."""
    output = StringIO()
    yaml.dump(data, output)
    rewritten = output.getvalue()

    if not source.endswith(("\n", "\r")):
        rewritten = rewritten.rstrip("\r\n")
    return rewritten


def plan_file(
    path: Path,
    github: GitHub,
    *,
    update: bool,
    upgrade: UpgradeLevel | None,
    include_prereleases: bool,
) -> FileEdit:
    """Plan all edits for one file entirely in memory without touching disk."""
    source, data, yaml = load_document(path)
    changes = pin_node(
        data,
        path,
        github,
        update=update,
        upgrade=upgrade,
        include_prereleases=include_prereleases,
    )
    rewritten = dump_document(source, data, yaml) if changes else source
    return FileEdit(path, source, rewritten, tuple(changes))


def yaml_files(paths: list[Path]) -> list[Path]:
    """Expand requested YAML files/directories, defaulting to the `.github` tree."""
    requested = paths or [Path(".github")]
    files: set[Path] = set()

    for requested_path in requested:
        path = requested_path.resolve()
        if path.is_dir():
            files.update(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in YAML_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in YAML_SUFFIXES:
            files.add(path)
        else:
            raise PinError(f"Not a YAML file or directory: {requested_path}")

    return sorted(files, key=str)


def atomic_write(path: Path, text: str) -> None:
    """Replace a file atomically while retaining its existing permission bits."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def location(path: Path, line: int | None) -> str:
    """Format a path and optional line number for human-readable tables."""
    return f"{path}:{line}" if line is not None else str(path)


def current_report_label(report: VersionReport) -> str:
    """Format the concrete current version, retaining a broad source alias as context."""
    if report.current is None:
        return report.source_ref or "—"
    if report.source_ref and report.source_ref != report.current.name:
        return f"{report.current.name} ({report.source_ref})"
    return report.current.name


def report_candidate(
    report: VersionReport,
    level: UpgradeLevel,
) -> str:
    """Return only a meaningful newer version for one report table column."""
    if report.current_version is None:
        return "—"

    candidate = {
        UpgradeLevel.PATCH: report.latest_patch,
        UpgradeLevel.MINOR: report.latest_minor,
        UpgradeLevel.MAJOR: report.latest_major,
    }[level]
    if candidate is None:
        return "—"

    if level == UpgradeLevel.MINOR and candidate.version.minor == report.current_version.minor:
        return "—"
    if level == UpgradeLevel.MAJOR and candidate.version.major == report.current_version.major:
        return "—"
    return candidate.name


def show_version_reports(reports: list[VersionReport]) -> None:
    """Render semantic-version freshness information as a Rich table and summary."""
    table = Table(title="GitHub Actions version report")
    table.add_column("Location", overflow="fold")
    table.add_column("Action")
    table.add_column("Current")
    table.add_column("Patch")
    table.add_column("Minor")
    table.add_column("Major")

    for report in reports:
        if report.note:
            table.add_row(
                location(report.path, report.line),
                report.action,
                report.note,
                "—",
                "—",
                "—",
            )
            continue

        table.add_row(
            location(report.path, report.line),
            report.action,
            current_report_label(report),
            report_candidate(report, UpgradeLevel.PATCH),
            report_candidate(report, UpgradeLevel.MINOR),
            report_candidate(report, UpgradeLevel.MAJOR),
        )

    console.print(table)

    semantic = [report for report in reports if report.current_version is not None]
    outdated = [
        report
        for report in semantic
        if any(
            candidate != "—"
            for candidate in (
                report_candidate(report, UpgradeLevel.PATCH),
                report_candidate(report, UpgradeLevel.MINOR),
                report_candidate(report, UpgradeLevel.MAJOR),
            )
        )
    ]
    skipped = len(reports) - len(semantic)
    summary = (
        f"{len(outdated)} of {len(semantic)} semantic action reference(s) "
        "have newer versions available"
    )
    if skipped:
        summary += f"; {skipped} non-semantic/unannotated reference(s) skipped"
    console.print(f"\n{summary}.")


def short_sha(value: str | None) -> str:
    """Return a compact commit identifier suitable for a summary table."""
    return value[:12] if value else "—"


def show_action_changes(changes: list[ActionChange], *, planned: bool) -> None:
    """Render a concise summary of action changes, especially semantic upgrades."""
    if not changes:
        return

    table = Table(title="Planned action changes" if planned else "Action changes")
    table.add_column("Location", overflow="fold")
    table.add_column("Action")
    table.add_column("Version/ref")
    table.add_column("Commit")

    for change in changes:
        if change.old_version and change.new_version:
            version_change = f"{change.old_version} → {change.new_version}"
        else:
            old_display = change.old_ref if not SHA.fullmatch(change.old_ref) else "pinned"
            version_change = f"{old_display} → {change.new_ref}"

        table.add_row(
            location(change.path, change.line),
            change.action,
            version_change,
            f"{short_sha(change.old_commit)} → {short_sha(change.new_commit)}",
        )

    console.print(table)


def show_diff(edits: list[FileEdit]) -> None:
    """Print unified diffs for all changed files without Rich markup or styling."""
    for edit in edits:
        diff = difflib.unified_diff(
            edit.before.splitlines(keepends=True),
            edit.after.splitlines(keepends=True),
            fromfile=str(edit.path),
            tofile=str(edit.path),
        )
        console.file.writelines(diff)


def build_github(api_url: str) -> httpx.Client:
    """Create the configured GitHub API client, using standard token environment vars."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pin-github-actions",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return httpx.Client(
        base_url=api_url.rstrip("/") + "/",
        headers=headers,
        timeout=30,
        follow_redirects=False,
    )


def validate_modes(
    *,
    check: bool,
    diff: bool,
    report: bool,
    update: bool,
    upgrade: UpgradeLevel | None,
    include_prereleases: bool,
) -> None:
    """Validate option combinations before reading files or contacting GitHub."""
    selected_output_modes = sum((check, diff, report))
    if selected_output_modes > 1:
        raise PinError("Only one of --check, --diff, or --report may be used")
    if update and upgrade is not None:
        raise PinError("--update and --upgrade are mutually exclusive")
    if (check or report) and (update or upgrade is not None):
        mode = "--check" if check else "--report"
        raise PinError(f"{mode} cannot be combined with --update or --upgrade")
    if include_prereleases and not (upgrade is not None or report):
        raise PinError("--include-prereleases requires --upgrade or --report")


def run_check(paths: list[Path]) -> int:
    """Check for floating action references without making any network requests."""
    findings = [
        (path, value)
        for path in paths
        for value in find_unpinned(load_document(path)[1])
    ]

    if findings:
        table = Table(title="Unpinned GitHub Actions")
        table.add_column("File", overflow="fold")
        table.add_column("Reference")
        for path, value in findings:
            table.add_row(str(path), value)
        console.print(table)
        console.print(f"\nFound {len(findings)} unpinned action reference(s).")
        return 1

    console.print(f"All action references are pinned across {len(paths)} file(s).")
    return 0


def run_report(
    paths: list[Path],
    github: GitHub,
    *,
    include_prereleases: bool,
) -> int:
    """Fetch and display available semantic-version updates without writing files."""
    reports = [
        report
        for path in paths
        for report in collect_version_reports(
            load_document(path)[1],
            path,
            github,
            include_prereleases=include_prereleases,
        )
    ]
    show_version_reports(reports)
    return 0


def run_changes(
    paths: list[Path],
    github: GitHub,
    *,
    diff: bool,
    update: bool,
    upgrade: UpgradeLevel | None,
    include_prereleases: bool,
) -> int:
    """Plan all pin/update/upgrade changes, then preview or atomically write them."""
    # Resolve and serialize every file first. If any API request or YAML operation
    # fails, no file has been touched and the repository cannot be left half-updated.
    edits = [
        plan_file(
            path,
            github,
            update=update,
            upgrade=upgrade,
            include_prereleases=include_prereleases,
        )
        for path in paths
    ]
    changed = [edit for edit in edits if edit.replacements]

    if diff:
        show_diff(changed)
    else:
        for edit in changed:
            atomic_write(edit.path, edit.after)

    changes = [change for edit in changed for change in edit.changes]
    if upgrade is not None:
        show_action_changes(changes, planned=diff)

    verb = "Would change" if diff else "Changed"
    console.print(
        f"{verb} {len(changes)} action reference(s) in {len(changed)} file(s)."
    )
    return 0


@app.command()
def main(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="YAML files or directories to scan. Defaults to [bold].github[/bold].",
            show_default=False,
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Fail if any external action is not pinned to a full SHA.",
            rich_help_panel="Modes",
        ),
    ] = False,
    diff: Annotated[
        bool,
        typer.Option(
            "--diff",
            help="Preview the exact YAML changes without writing files.",
            rich_help_panel="Modes",
        ),
    ] = False,
    report: Annotated[
        bool,
        typer.Option(
            "--report",
            help="Report current, patch, minor, and major semantic versions.",
            rich_help_panel="Modes",
        ),
    ] = False,
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Re-resolve existing SHA pins using their annotated refs.",
            rich_help_panel="Update controls",
        ),
    ] = False,
    upgrade: Annotated[
        UpgradeLevel | None,
        typer.Option(
            "--upgrade",
            help="Upgrade semantic v-tags within the selected boundary.",
            rich_help_panel="Update controls",
        ),
    ] = None,
    include_prereleases: Annotated[
        bool,
        typer.Option(
            "--include-prereleases",
            help="Include prerelease tags in --report or --upgrade selection.",
            rich_help_panel="Update controls",
        ),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option(
            "--api-url",
            help="GitHub API base URL; defaults to GITHUB_API_URL or github.com.",
            envvar="GITHUB_API_URL",
            rich_help_panel="GitHub",
        ),
    ] = "https://api.github.com",
) -> None:
    """Pin GitHub Actions, check pins, report freshness, or perform SemVer upgrades."""
    try:
        validate_modes(
            check=check,
            diff=diff,
            report=report,
            update=update,
            upgrade=upgrade,
            include_prereleases=include_prereleases,
        )

        files = yaml_files(paths or [])
        if not files:
            console.print("No YAML files found.")
            return

        if check:
            raise typer.Exit(run_check(files))

        with build_github(api_url) as client:
            github = GitHub(client)
            if report:
                raise typer.Exit(
                    run_report(
                        files,
                        github,
                        include_prereleases=include_prereleases,
                    )
                )

            raise typer.Exit(
                run_changes(
                    files,
                    github,
                    diff=diff,
                    update=update,
                    upgrade=upgrade,
                    include_prereleases=include_prereleases,
                )
            )
    except typer.Exit:
        raise
    except (PinError, YAMLError, OSError) as exc:
        error_console.print(f"[bold red]error:[/bold red] {exc}")
        raise typer.Exit(2) from None


if __name__ == "__main__":
    app()