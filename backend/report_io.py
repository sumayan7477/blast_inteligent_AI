"""
Safe read/write helpers for the local JSON/Markdown reports in ``local_reports/``.

* ``load_json_report`` never echoes file contents in its errors - only the
  file name and the parser's line/column.
* ``confirm_overwrite`` asks before replacing existing reports (or refuses
  when not interactive and ``--force`` was not given).
* ``write_text_atomic`` writes via a temp file and can keep a ``.bak`` copy.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


class ReportError(Exception):
    """A report is missing or unusable. The message contains no data values."""


def load_json_report(path: Path, *, what: str = "report") -> Any:
    """Load a JSON report, raising ``ReportError`` with a value-free message."""
    if not path.exists():
        raise ReportError(f"{what} not found: {path}")
    if not path.is_file():
        raise ReportError(f"{what} is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ReportError(f"{what} is not valid UTF-8: {path.name}") from None
    except OSError as exc:
        raise ReportError(f"Cannot read {what} {path.name} ({type(exc).__name__})") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # exc.msg is a parser message such as "Expecting ',' delimiter" - no content.
        raise ReportError(
            f"{what} {path.name} is malformed JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from None


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm_overwrite(paths: list[Path], *, force: bool) -> bool:
    """Return True if it is OK to write ``paths``.

    Existing files are only replaced when ``force`` is set or the user answers
    'y' at an interactive prompt.
    """
    existing = [p for p in paths if p.exists()]
    if not existing or force:
        return True
    print("\nThese reports already exist and would be overwritten:")
    for p in existing:
        print(f"  - {p}")
    if not is_interactive():
        print("Not an interactive terminal: refusing to overwrite. Re-run with --force.")
        return False
    try:
        answer = input("Overwrite them? A .bak copy of each is kept. [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def write_text_atomic(path: Path, text: str, *, backup: bool = True) -> None:
    """Write ``text`` to ``path`` atomically; keep ``<name>.bak`` of an old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: Any, *, backup: bool = True) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", backup=backup)
