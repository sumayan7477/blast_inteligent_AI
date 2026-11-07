"""
Privacy-safe schema profiler for the confidential JSONL files in ``data/``.

What it does
------------
For every ``*.jsonl`` file (processed independently, streamed line by line):

* counts total / blank / valid / invalid / non-object lines
* discovers every field path, including nested ones
  (``a.b`` for objects, ``a[]`` for array elements)
* records the JSON types seen per field
* counts missing values (absent, ``null``, empty string)
* flags numeric fields and numeric-looking strings
* flags date-like fields (ISO-8601 / common date strings, epoch numbers)
* estimates cardinality (distinct-value count) using in-memory hashes only
* records array-length and string-length *statistics* (never contents)

What it NEVER does
------------------
* store, print or log any record value (not even min/max - those are values)
* store distinct values - only Python ``hash()`` ints, held in memory and
  discarded; only the resulting *count* is written
* include JSON parser error snippets (only error type, line and column)
* send anything over the network

The report is written to ``local_reports/`` (Git-ignored).

Usage
-----
    python backend/data_inspector.py                  # all files
    python backend/data_inspector.py --max-records 1000   # quick smoke test
    python backend/data_inspector.py --files multifield.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running as ``python backend/data_inspector.py`` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

REPORT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Value classification helpers (operate on values, return only labels)
# ---------------------------------------------------------------------------
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO-8601 date / datetime, optional fraction and timezone
    re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"),
    # 2024/01/31 or 2024.01.31 (+ optional time)
    re.compile(r"^\d{4}[/.]\d{1,2}[/.]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"),
    # 31/01/2024, 01-31-2024, 31.01.2024 (+ optional time)
    re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"),
    # Time only: 13:45 or 13:45:10
    re.compile(r"^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$"),
)
_NUMERIC_STRING = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

# Plausible Unix-epoch ranges (roughly years 2001-2033).
_EPOCH_SECONDS = (1.0e9, 2.0e9)
_EPOCH_MILLIS = (1.0e12, 2.0e12)

_DATE_NAME_HINTS = ("date", "time", "timestamp", "created", "updated", "modified", "_at", "start", "end")


def _json_type(value: Any) -> str:
    """Return the JSON type label of ``value`` (``bool`` checked before ``int``)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_date_string(value: str) -> bool:
    s = value.strip()
    if not 4 <= len(s) <= 40:
        return False
    return any(p.match(s) for p in _DATE_PATTERNS)


def _epoch_kind(value: float) -> str | None:
    if _EPOCH_SECONDS[0] <= value <= _EPOCH_SECONDS[1]:
        return "seconds"
    if _EPOCH_MILLIS[0] <= value <= _EPOCH_MILLIS[1]:
        return "milliseconds"
    return None


# ---------------------------------------------------------------------------
# Running statistics containers
# ---------------------------------------------------------------------------
@dataclass
class _LengthStats:
    """Count / min / max / mean of a *length* (not a value)."""

    count: int = 0
    total: int = 0
    min: int | None = None
    max: int | None = None

    def add(self, n: int) -> None:
        self.count += 1
        self.total += n
        self.min = n if self.min is None or n < self.min else self.min
        self.max = n if self.max is None or n > self.max else self.max

    def to_dict(self) -> dict[str, Any] | None:
        if not self.count:
            return None
        return {
            "min": self.min,
            "max": self.max,
            "mean": round(self.total / self.count, 2),
        }


@dataclass
class FieldStats:
    """Aggregated, value-free statistics for one field path."""

    path: str
    depth: int
    occurrences: int = 0            # times the path was encountered (arrays may repeat)
    records_present: int = 0        # records where the key exists
    records_non_null: int = 0       # records with at least one non-null, non-empty value
    null_count: int = 0
    empty_string_count: int = 0
    type_counts: dict[str, int] = field(default_factory=dict)
    numeric_string_count: int = 0
    date_string_count: int = 0
    epoch_seconds_count: int = 0
    epoch_millis_count: int = 0
    non_finite_count: int = 0       # NaN / Infinity
    integral_float_count: int = 0   # floats with no fractional part
    string_length: _LengthStats = field(default_factory=_LengthStats)
    array_length: _LengthStats = field(default_factory=_LengthStats)
    object_key_count: _LengthStats = field(default_factory=_LengthStats)
    _hashes: set[int] = field(default_factory=set, repr=False)
    distinct_capped: bool = False

    def observe(self, value: Any, distinct_cap: int) -> None:
        """Update counters for one occurrence. ``value`` is never stored."""
        self.occurrences += 1
        jtype = _json_type(value)
        self.type_counts[jtype] = self.type_counts.get(jtype, 0) + 1

        if jtype == "null":
            self.null_count += 1
            return
        if jtype == "string":
            self.string_length.add(len(value))
            if value == "":
                self.empty_string_count += 1
            else:
                if _NUMERIC_STRING.match(value.strip()):
                    self.numeric_string_count += 1
                if _is_date_string(value):
                    self.date_string_count += 1
        elif jtype in ("integer", "float"):
            num = float(value)
            if not math.isfinite(num):
                self.non_finite_count += 1
            else:
                if jtype == "float" and num.is_integer():
                    self.integral_float_count += 1
                kind = _epoch_kind(num)
                if kind == "seconds":
                    self.epoch_seconds_count += 1
                elif kind == "milliseconds":
                    self.epoch_millis_count += 1
        elif jtype == "array":
            self.array_length.add(len(value))
            return
        elif jtype == "object":
            self.object_key_count.add(len(value))
            return

        # Cardinality for scalars only: keep a hash, never the value.
        if not self.distinct_capped:
            self._hashes.add(hash((jtype, value)))
            if len(self._hashes) > distinct_cap:
                self.distinct_capped = True
                self._hashes.clear()

    # -- report -------------------------------------------------------------
    def _infer_role(self, valid_records: int) -> str:
        scalar_types = {t: c for t, c in self.type_counts.items() if t != "null"}
        if not scalar_types:
            return "always_null"
        dominant = max(scalar_types, key=scalar_types.get)
        non_null = sum(scalar_types.values())

        if dominant == "object":
            return "nested_object"
        if dominant == "array":
            return "array"
        if dominant == "boolean":
            return "boolean"

        distinct = None if self.distinct_capped else len(self._hashes)
        name = self.path.lower()

        if dominant in ("integer", "float"):
            epoch = self.epoch_seconds_count + self.epoch_millis_count
            if epoch >= 0.9 * non_null and any(h in name for h in _DATE_NAME_HINTS):
                return "date_like_epoch"
            if distinct is not None and distinct <= 2:
                return "binary_flag_numeric"
            return "numeric"

        if dominant == "string":
            strings = scalar_types["string"] - self.empty_string_count
            if strings and self.date_string_count >= 0.9 * strings:
                return "date_like_string"
            if strings and self.numeric_string_count >= 0.9 * strings:
                return "numeric_string"
            if distinct is not None and distinct <= 50:
                return "categorical"
            if self.distinct_capped or (distinct and distinct >= 0.95 * max(non_null, 1)):
                mean_len = (self.string_length.total / self.string_length.count) if self.string_length.count else 0
                return "free_text" if mean_len > 60 else "identifier_like"
            return "string"
        return dominant

    def to_dict(self, valid_records: int) -> dict[str, Any]:
        missing = max(valid_records - self.records_non_null, 0)
        distinct = f">{config.INSPECTOR_DISTINCT_CAP}" if self.distinct_capped else len(self._hashes)
        return {
            "path": self.path,
            "depth": self.depth,
            "inferred_role": self._infer_role(valid_records),
            "types": dict(sorted(self.type_counts.items(), key=lambda kv: -kv[1])),
            "is_nested": self.depth > 0 or "[]" in self.path,
            "occurrences": self.occurrences,
            "records_present": self.records_present,
            "records_missing_or_empty": missing,
            "missing_pct": round(100 * missing / valid_records, 2) if valid_records else None,
            "null_count": self.null_count,
            "empty_string_count": self.empty_string_count,
            "distinct_scalar_values": distinct,
            "numeric_string_count": self.numeric_string_count,
            "date_string_count": self.date_string_count,
            "epoch_seconds_like_count": self.epoch_seconds_count,
            "epoch_millis_like_count": self.epoch_millis_count,
            "integral_float_count": self.integral_float_count,
            "non_finite_count": self.non_finite_count,
            "string_length": self.string_length.to_dict(),
            "array_length": self.array_length.to_dict(),
            "object_key_count": self.object_key_count.to_dict(),
        }


# ---------------------------------------------------------------------------
# File profiler
# ---------------------------------------------------------------------------
class FileProfiler:
    """Streams one JSONL file and accumulates value-free statistics."""

    MAX_ERROR_SAMPLES = 25

    def __init__(
        self,
        path: Path,
        *,
        max_depth: int,
        max_array_items: int,
        max_fields: int,
        distinct_cap: int,
        max_records: int | None,
        progress_seconds: float,
    ) -> None:
        self.path = path
        self.max_depth = max_depth
        self.max_array_items = max_array_items
        self.max_fields = max_fields
        self.distinct_cap = distinct_cap
        self.max_records = max_records
        self.progress_seconds = progress_seconds

        self.fields: dict[str, FieldStats] = {}
        self.fields_overflow = 0
        self.depth_truncations = 0
        self.array_items_skipped = 0

        self.total_lines = 0
        self.blank_lines = 0
        self.valid_records = 0
        self.invalid_json = 0
        self.invalid_encoding = 0
        self.non_object_records = 0
        self.non_object_types: dict[str, int] = {}
        self.error_samples: list[dict[str, Any]] = []
        self.top_level_key_count = _LengthStats()

    # -- traversal ------------------------------------------------------------
    def _field(self, path: str, depth: int) -> FieldStats | None:
        stats = self.fields.get(path)
        if stats is None:
            if len(self.fields) >= self.max_fields:
                self.fields_overflow += 1
                return None
            stats = self.fields[path] = FieldStats(path=path, depth=depth)
        return stats

    def _walk(self, value: Any, path: str, depth: int, present: set[str], non_null: set[str]) -> None:
        stats = self._field(path, depth)
        if stats is not None:
            stats.observe(value, self.distinct_cap)
            present.add(path)
            if value is not None and value != "" and value != [] and value != {}:
                non_null.add(path)

        if depth >= self.max_depth:
            if isinstance(value, (dict, list)) and value:
                self.depth_truncations += 1
            return
        if isinstance(value, dict):
            for key, child in value.items():
                self._walk(child, f"{path}.{key}", depth + 1, present, non_null)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if i >= self.max_array_items:
                    self.array_items_skipped += len(value) - i
                    break
                self._walk(item, f"{path}[]", depth + 1, present, non_null)

    def _profile_record(self, record: dict[str, Any]) -> None:
        present: set[str] = set()
        non_null: set[str] = set()
        self.top_level_key_count.add(len(record))
        for key, value in record.items():
            self._walk(value, str(key), 0, present, non_null)
        for p in present:
            self.fields[p].records_present += 1
        for p in non_null:
            self.fields[p].records_non_null += 1

    def _record_error(self, line_no: int, kind: str, detail: str, column: int | None = None) -> None:
        if len(self.error_samples) < self.MAX_ERROR_SAMPLES:
            # ``detail`` is the parser's message only (e.g. "Expecting value"), never the line text.
            self.error_samples.append({"line": line_no, "kind": kind, "detail": detail, "column": column})

    # -- main loop ------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        size = self.path.stat().st_size
        started = time.perf_counter()
        last_report = started
        bytes_read = 0

        with self.path.open("rb") as fh:  # binary: exact byte progress + explicit decoding
            for line_no, raw in enumerate(fh, start=1):
                bytes_read += len(raw)
                self.total_lines += 1

                now = time.perf_counter()
                if now - last_report >= self.progress_seconds:
                    _print_progress(self.path.name, bytes_read, size, self.valid_records, now - started)
                    last_report = now

                try:
                    text = raw.decode("utf-8-sig" if line_no == 1 else "utf-8")
                except UnicodeDecodeError as exc:
                    self.invalid_encoding += 1
                    self._record_error(line_no, "encoding", exc.reason)
                    continue

                if not text.strip():
                    self.blank_lines += 1
                    continue

                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    self.invalid_json += 1
                    self._record_error(line_no, "json", exc.msg, exc.colno)
                    continue
                except RecursionError:
                    self.invalid_json += 1
                    self._record_error(line_no, "json", "nesting too deep")
                    continue

                if not isinstance(record, dict):
                    self.non_object_records += 1
                    t = _json_type(record)
                    self.non_object_types[t] = self.non_object_types.get(t, 0) + 1
                    continue

                self.valid_records += 1
                self._profile_record(record)

                if self.max_records and self.valid_records >= self.max_records:
                    break

        elapsed = time.perf_counter() - started
        _print_progress(self.path.name, bytes_read, size, self.valid_records, elapsed, final=True)
        return self._build_report(size, bytes_read, elapsed)

    def _build_report(self, size: int, bytes_read: int, elapsed: float) -> dict[str, Any]:
        fields = [f.to_dict(self.valid_records) for f in sorted(self.fields.values(), key=lambda f: f.path)]
        top_level = [f["path"] for f in fields if f["depth"] == 0]
        by_role: dict[str, list[str]] = {}
        for f in fields:
            by_role.setdefault(f["inferred_role"], []).append(f["path"])

        return {
            "file_name": self.path.name,
            "file_size_bytes": size,
            "bytes_scanned": bytes_read,
            "complete_scan": bytes_read >= size,
            "elapsed_seconds": round(elapsed, 2),
            "line_counts": {
                "total_lines": self.total_lines,
                "blank_lines": self.blank_lines,
                "valid_object_records": self.valid_records,
                "invalid_json": self.invalid_json,
                "invalid_encoding": self.invalid_encoding,
                "non_object_records": self.non_object_records,
                "non_object_types": self.non_object_types,
            },
            "error_samples": self.error_samples,
            "schema_summary": {
                "distinct_field_paths": len(fields),
                "top_level_field_count": len(top_level),
                "top_level_fields": top_level,
                "nested_field_count": sum(1 for f in fields if f["is_nested"]),
                "top_level_keys_per_record": self.top_level_key_count.to_dict(),
                "fields_by_inferred_role": by_role,
            },
            "limits": {
                "max_depth": self.max_depth,
                "max_array_items_examined": self.max_array_items,
                "max_fields": self.max_fields,
                "distinct_cap": self.distinct_cap,
                "fields_overflow_events": self.fields_overflow,
                "depth_truncations": self.depth_truncations,
                "array_items_skipped": self.array_items_skipped,
            },
            "fields": fields,
        }


# ---------------------------------------------------------------------------
# CLI / orchestration
# ---------------------------------------------------------------------------
def _print_progress(name: str, done: int, total: int, records: int, elapsed: float, final: bool = False) -> None:
    pct = 100.0 * done / total if total else 100.0
    rate = done / elapsed / 1e6 if elapsed > 0 else 0.0
    end = "\n" if final else "\r"
    status = "done" if final else "...."
    print(f"  [{status}] {name}: {pct:5.1f}%  {records:,} records  {rate:6.1f} MB/s  {elapsed:6.1f}s",
          end=end, file=sys.stderr, flush=True)


def discover_files(data_dir: Path, only: Iterable[str] | None = None) -> list[Path]:
    """Return JSONL files in ``data_dir`` (optionally filtered by name), sorted by name."""
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")
    files = sorted(p for p in data_dir.glob(config.DATA_FILE_GLOB) if p.is_file())
    if only:
        wanted = set(only)
        missing = wanted - {p.name for p in files}
        if missing:
            raise FileNotFoundError(f"Requested files not found in data dir: {sorted(missing)}")
        files = [p for p in files if p.name in wanted]
    return files


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Privacy-safe schema profiler for local JSONL data.")
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--output-dir", type=Path, default=config.REPORTS_DIR)
    p.add_argument("--files", nargs="+", metavar="NAME", help="Only profile these file names.")
    p.add_argument("--max-records", type=int, default=None,
                   help="Stop each file after N valid records (quick test). Default: full scan.")
    p.add_argument("--max-depth", type=int, default=config.INSPECTOR_MAX_DEPTH)
    p.add_argument("--max-array-items", type=int, default=config.INSPECTOR_MAX_ARRAY_ITEMS)
    p.add_argument("--max-fields", type=int, default=config.INSPECTOR_MAX_FIELDS_PER_FILE)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        files = discover_files(args.data_dir.resolve(), args.files)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not files:
        print(f"No {config.DATA_FILE_GLOB} files found in {args.data_dir}", file=sys.stderr)
        return 1

    started_at = datetime.now(timezone.utc)
    print(f"Profiling {len(files)} file(s). Report -> {output_dir}", file=sys.stderr)

    file_reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for i, path in enumerate(files, start=1):
        print(f"\n({i}/{len(files)}) {path.name}  [{path.stat().st_size / 1e6:,.1f} MB]", file=sys.stderr)
        try:
            profiler = FileProfiler(
                path,
                max_depth=args.max_depth,
                max_array_items=args.max_array_items,
                max_fields=args.max_fields,
                distinct_cap=config.INSPECTOR_DISTINCT_CAP,
                max_records=args.max_records,
                progress_seconds=config.INSPECTOR_PROGRESS_SECONDS,
            )
            report = profiler.run()
            file_reports.append(report)
            # Per-file report so a crash later on doesn't lose earlier work.
            _write_json_atomic(output_dir / f"profile_{path.stem}.json", report)
        except KeyboardInterrupt:
            print("\nInterrupted by user.", file=sys.stderr)
            break
        except Exception as exc:  # keep going with remaining files
            # Only the exception *type* is reported, to avoid echoing data in messages.
            failures.append({"file_name": path.name, "error_type": type(exc).__name__})
            print(f"  FAILED: {type(exc).__name__} (continuing)", file=sys.stderr)

    combined = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": started_at.isoformat(timespec="seconds"),
        "privacy": "Contains field names, types and aggregate counts only. No record values.",
        "data_dir": str(args.data_dir),
        "max_records_per_file": args.max_records,
        "files_profiled": len(file_reports),
        "files_failed": failures,
        "overview": [
            {
                "file_name": r["file_name"],
                "file_size_bytes": r["file_size_bytes"],
                "valid_records": r["line_counts"]["valid_object_records"],
                "invalid_lines": r["line_counts"]["invalid_json"] + r["line_counts"]["invalid_encoding"],
                "distinct_field_paths": r["schema_summary"]["distinct_field_paths"],
                "top_level_fields": r["schema_summary"]["top_level_field_count"],
                "complete_scan": r["complete_scan"],
            }
            for r in file_reports
        ],
        "files": file_reports,
    }
    out_path = output_dir / "data_profile.json"
    _write_json_atomic(out_path, combined)

    # Console summary: counts only.
    print("\nSummary", file=sys.stderr)
    for row in combined["overview"]:
        print(f"  {row['file_name']:<32} records={row['valid_records']:>12,}  invalid={row['invalid_lines']:>8,}"
              f"  fields={row['distinct_field_paths']:>5}", file=sys.stderr)
    if failures:
        print(f"  {len(failures)} file(s) failed: {[f['file_name'] for f in failures]}", file=sys.stderr)
    print(f"\nReport written to {out_path}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
