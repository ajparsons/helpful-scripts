#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "polib>=1.2",
#   "typer>=0.12",
# ]
# ///

from pathlib import Path

import polib
import typer

app = typer.Typer()


@app.command()
def main(
    po_file: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="PO file to inspect.",
    ),
    output: Path = typer.Option(
        Path("missing.txt"),
        "--output",
        "-o",
        help="Output text file.",
    ),
    include_fuzzy: bool = typer.Option(
        False,
        "--include-fuzzy",
        help="Also include strings with fuzzy translations.",
    ),
) -> None:
    """Extract untranslated strings from a PO file."""

    po = polib.pofile(po_file)

    strings: list[str] = []

    for entry in po:
        if entry.obsolete or not entry.msgid:
            continue

        is_fuzzy = "fuzzy" in entry.flags
        is_missing = not entry.translated()

        if is_missing or (include_fuzzy and is_fuzzy):
            strings.append(entry.msgid)

    # Three blank lines between each item.
    output.write_text(
        "\n\n\n\n".join(strings) + ("\n" if strings else ""),
        encoding="utf-8",
    )

    typer.echo(f"Wrote {len(strings)} strings to {output}")


if __name__ == "__main__":
    app()