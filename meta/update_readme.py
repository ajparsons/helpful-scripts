#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Update README.md with a table of scripts and the SHA of the last commit
that modified each one.

Expects to be run from the repo root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
REPO_URL = "https://github.com/ajparsons/helpful-scripts"

START_MARKER = "<!-- SCRIPT_TABLE_START -->"
END_MARKER = "<!-- SCRIPT_TABLE_END -->"


def get_scripts() -> dict[str, list[Path]]:
    """
    Find all .py scripts in the repo (excluding meta/), grouped by their
    top-level directory. Scripts in the repo root go under "Other".
    """
    groups: dict[str, list[Path]] = {}
    for p in sorted(REPO_ROOT.rglob("*.py")):
        parts = p.relative_to(REPO_ROOT).parts
        if "meta" in parts:
            continue
        folder = parts[0] if len(parts) > 1 else "Other"
        groups.setdefault(folder, []).append(p)
    return groups


def last_commit_sha(path: Path) -> str:
    """
    Return the full SHA of the most recent commit that touched *path*.
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    return result.stdout.strip()


def folder_heading(folder: str) -> str:
    """
    Turn a folder name like 'repo-management' into a heading like
    'Repo management'.
    """
    return folder.replace("-", " ").replace("_", " ").capitalize()


def build_sections(groups: dict[str, list[Path]]) -> str:
    """
    Build a Markdown section per folder, each with a heading and table.
    """
    sections: list[str] = []
    for folder, scripts in groups.items():
        lines = [
            f"## {folder_heading(folder)}",
            "",
            "| Script | Run | Last updated |",
            "| ------ | --- | ------------ |",
        ]
        for script in scripts:
            rel = script.relative_to(REPO_ROOT)
            sha = last_commit_sha(script)
            short_sha = sha[:7]
            raw_url = f"{REPO_URL}/raw/{sha}/{rel}"
            run_cmd = f"`uv run {raw_url}`"
            commit_url = f"{REPO_URL}/commit/{sha}"
            lines.append(
                f"| [{rel.name}]({rel}) | {run_cmd} | [{short_sha}]({commit_url}) |"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def update_readme(body: str) -> None:
    """
    Replace the content between the start/end markers in README.md with the
    generated sections. If markers are missing, append them.
    """
    content = README.read_text()

    new_block = f"{START_MARKER}\n{body}\n{END_MARKER}"

    if START_MARKER in content and END_MARKER in content:
        before = content[: content.index(START_MARKER)]
        after = content[content.index(END_MARKER) + len(END_MARKER) :]
        content = before + new_block + after
    else:
        content = content.rstrip() + "\n\n" + new_block + "\n"

    README.write_text(content)


def main() -> None:
    groups = get_scripts()
    if not groups:
        print("No scripts found.")
        return
    sections = build_sections(groups)
    update_readme(sections)
    total = sum(len(s) for s in groups.values())
    print(f"Updated README.md with {total} script(s) in {len(groups)} section(s).")


if __name__ == "__main__":
    main()
