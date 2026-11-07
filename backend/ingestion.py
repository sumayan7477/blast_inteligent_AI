"""
Streaming, resumable JSONL -> DuckDB ingestion.

How it works
------------
* Each JSONL file is mapped to its own raw table (see ``database.py``). The
  mapping is read from ``backend/ingestion_config.json`` (Git-ignored); any
  file without an entry gets safe defaults, so nothing about field names has
  to be known in advance.
* Files are streamed in binary mode, one line at a time. Only one batch
  (``batch_size`` records) is held in memory.
* Every batch is written in **one transaction** together with the file's
  resume offset in ``meta_ingestion_files``. If the process stops, the next
  run seeks to the last committed byte offset and continues.
* Duplicates are prevented by ``_record_key`` (PRIMARY KEY + ``ON CONFLICT DO
  NOTHING``), so re-running, ``--restart`` or overlapping runs never create
  duplicate rows:
    - ``content_hash`` (default): key = SHA-256 of the canonical JSON record.
      Byte-identical records are stored once.
    - ``source_id``: key = SHA-256 of a configured id field. First record
      with a given id wins. Use only if the profile shows the id is unique.
* Records are stored with sorted keys (canonical JSON). Non-finite numbers
  (NaN/Infinity), which DuckDB's JSON type rejects, are stored as null and
  counted in ``non_finite_replaced``.

Privacy
-------
Logs and metadata contain only file names, table names, counts, line numbers
and parser error *types/messages*. Record contents are never logged.

Usage
-----
    python backend/ingestion.py --list                     # show plan, read nothing
    python backend/ingestion.py --files multifield.jsonl --max-records 1000
    python backend/ingestion.py                            # ingest / resume all
    python backend/ingestion.py --restart                  # rescan from byte 0 (no duplicates)
    python backend/ingestion.py --rebuild --files X.jsonl  # drop table X and reload
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
from logging_setup import safe_error, setup_logging  # noqa: E402

log = logging.getLogger("ingestion")

DEDUPE_STRATEGIES: tuple[str, ...] = ("content_hash", "source_id")
_FILE_ENTRY_KEYS = {"enabled", "table", "dedupe_strategy", "source_id_field", "batch_size", "view", "columns"}
_DEFAULT_KEYS = {"batch_size", "dedupe_strategy"}


class ConfigError(ValueError):
    """Raised for an invalid ingestion configuration."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileMapping:
    """How one source file is loaded."""

    source_file: str
    table: str
    enabled: bool = True
    dedupe_strategy: str = "content_hash"
    source_id_field: tuple[db.PathSegment, ...] | None = None
    batch_size: int = config.INGEST_BATCH_SIZE
    view: str | None = None
    columns: tuple[db.ColumnSpec, ...] = ()


def default_table_name(file_name: str) -> str:
    """``foo-bar.jsonl`` -> ``raw_foo_bar`` (derived from the file name only)."""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(file_name).stem).strip("_").lower()
    if not stem or stem[0].isdigit():
        stem = f"f_{stem}"
    return f"{db.RAW_TABLE_PREFIX}{stem}"[:63]


def parse_path(value: Any, *, where: str) -> tuple[db.PathSegment, ...]:
    """Parse ``"a.b.0"`` or ``["a", "b", 0]`` into validated path segments.

    In the string form an all-digit segment is an array index; use the list
    form if a key is literally made of digits or contains a dot.
    """
    if isinstance(value, str):
        segs: tuple[db.PathSegment, ...] = tuple(int(p) if p.isdigit() else p for p in value.split("."))
    elif isinstance(value, list):
        segs = tuple(value)
    else:
        raise ConfigError(f"{where}: path must be a string or list.")
    try:
        db.json_path(segs)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    return segs


def _discover(data_dir: Path) -> list[Path]:
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")
    return sorted(p for p in data_dir.glob(config.DATA_FILE_GLOB) if p.is_file())


def _strip_comments(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def load_mappings(config_path: Path, data_dir: Path) -> list[FileMapping]:
    """Build one ``FileMapping`` per discovered file, applying the config if present."""
    files = _discover(data_dir)
    raw_cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{config_path.name}: invalid JSON at line {exc.lineno}") from None
        log.info("Using mapping config %s", config_path)
    else:
        log.info("No mapping config at %s - using defaults for every file.", config_path)

    defaults = _strip_comments(raw_cfg.get("defaults", {}) or {})
    unknown = set(defaults) - _DEFAULT_KEYS
    if unknown:
        raise ConfigError(f"defaults: unknown keys {sorted(unknown)}")
    file_cfgs = _strip_comments(raw_cfg.get("files", {}) or {})

    names = {p.name for p in files}
    for extra in sorted(set(file_cfgs) - names):
        log.warning("Config entry %r has no matching file in %s - ignored.", extra, data_dir)

    mappings: list[FileMapping] = []
    for path in files:
        entry = _strip_comments(file_cfgs.get(path.name, {}) or {})
        where = f"files[{path.name!r}]"
        unknown = set(entry) - _FILE_ENTRY_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")

        table = entry.get("table") or default_table_name(path.name)
        try:
            db.validate_raw_table_name(table)
        except ValueError as exc:
            raise ConfigError(f"{where}: {exc}") from None

        strategy = entry.get("dedupe_strategy", defaults.get("dedupe_strategy", "content_hash"))
        if strategy not in DEDUPE_STRATEGIES:
            raise ConfigError(f"{where}: dedupe_strategy must be one of {DEDUPE_STRATEGIES}")

        id_field = entry.get("source_id_field")
        id_path = parse_path(id_field, where=f"{where}.source_id_field") if id_field else None
        if strategy == "source_id" and id_path is None:
            raise ConfigError(f"{where}: dedupe_strategy 'source_id' requires source_id_field.")

        batch_size = int(entry.get("batch_size", defaults.get("batch_size", config.INGEST_BATCH_SIZE)))
        if batch_size < 1:
            raise ConfigError(f"{where}: batch_size must be >= 1")

        columns: list[db.ColumnSpec] = []
        for i, col in enumerate(entry.get("columns", []) or []):
            col = _strip_comments(col)
            try:
                columns.append(db.ColumnSpec(
                    name=col["name"],
                    path=parse_path(col["path"], where=f"{where}.columns[{i}].path"),
                    type=str(col.get("type", "VARCHAR")).upper(),
                ))
            except KeyError as exc:
                raise ConfigError(f"{where}.columns[{i}]: missing {exc}") from None
            except ValueError as exc:
                raise ConfigError(f"{where}.columns[{i}]: {exc}") from None

        view = entry.get("view")
        if columns and not view:
            view = "v_" + table.removeprefix(db.RAW_TABLE_PREFIX)
        if view:
            try:
                db.validate_identifier(view, kind="view name")
            except ValueError as exc:
                raise ConfigError(f"{where}: {exc}") from None
            if not columns:
                raise ConfigError(f"{where}: view {view!r} is set but no columns are configured.")

        mappings.append(FileMapping(
            source_file=path.name,
            table=table,
            enabled=bool(entry.get("enabled", True)),
            dedupe_strategy=strategy,
            source_id_field=id_path,
            batch_size=batch_size,
            view=view,
            columns=tuple(columns),
        ))

    for attr in ("table", "view"):
        seen: dict[str, str] = {}
        for m in mappings:
            name = getattr(m, attr)
            if name is None:
                continue
            if name.lower() in seen:
                raise ConfigError(f"{m.source_file} and {seen[name.lower()]} map to the same {attr} {name!r}.")
            seen[name.lower()] = m.source_file
    return mappings


def write_config_skeleton(config_path: Path, data_dir: Path, *, overwrite: bool = False) -> bool:
    """Write a mapping skeleton listing discovered file names (no field names).

    Returns False if the file already exists and ``overwrite`` is False.
    """
    if config_path.exists() and not overwrite:
        return False
    skeleton = {
        "_comment": "Local only (git-ignored). Fill source_id_field / columns from local_reports/data_profile.json.",
        "defaults": {"batch_size": config.INGEST_BATCH_SIZE, "dedupe_strategy": "content_hash"},
        "files": {
            p.name: {
                "enabled": True,
                "table": default_table_name(p.name),
                "dedupe_strategy": "content_hash",
                "source_id_field": None,
                "view": None,
                "columns": [],
            }
            for p in _discover(data_dir)
        },
    }
    config_path.write_text(json.dumps(skeleton, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Record helpers (values never leave these functions except into the DB)
# ---------------------------------------------------------------------------
def _get_path(obj: Any, segs: tuple[db.PathSegment, ...]) -> Any:
    for seg in segs:
        if isinstance(seg, int):
            if not isinstance(obj, list) or seg >= len(obj):
                return None
            obj = obj[seg]
        else:
            if not isinstance(obj, dict) or seg not in obj:
                return None
            obj = obj[seg]
    return obj


def _replace_non_finite(obj: Any) -> tuple[Any, int]:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None, 1
    if isinstance(obj, dict):
        total, out = 0, {}
        for k, v in obj.items():
            out[k], n = _replace_non_finite(v)
            total += n
        return out, total
    if isinstance(obj, list):
        total, items = 0, []
        for v in obj:
            item, n = _replace_non_finite(v)
            items.append(item)
            total += n
        return items, total
    return obj, 0


def _canonical_json(obj: Any) -> tuple[str, int]:
    """Deterministic JSON text (sorted keys, ASCII-escaped) + count of replaced NaN/Inf."""
    kwargs = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": True}
    try:
        return json.dumps(obj, allow_nan=False, **kwargs), 0
    except ValueError:
        clean, n = _replace_non_finite(obj)
        return json.dumps(clean, allow_nan=False, **kwargs), n


def _id_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _key(namespace: str, text: str) -> str:
    return hashlib.sha256(f"{namespace}\x1f{text}".encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Per-file ingestion
# ---------------------------------------------------------------------------
_COUNTERS = (
    "lines_processed", "records_seen", "records_inserted", "duplicates_skipped", "invalid_lines",
    "blank_lines", "non_object_lines", "missing_source_id", "non_finite_replaced",
)


@dataclass
class FileResult:
    source_file: str
    table: str
    status: str
    inserted_this_run: int = 0
    duplicates_this_run: int = 0
    invalid_this_run: int = 0
    elapsed_seconds: float = 0.0
    note: str | None = None


@dataclass
class _Batch:
    rows: dict[str, tuple[str, str, int, str | None, str]] = field(default_factory=dict)
    errors: list[tuple[str, int, int, str, str | None, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_COUNTERS, 0))


class FileIngestor:
    """Streams one file into its raw table with resumable, transactional batches."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        mapping: FileMapping,
        data_dir: Path,
        run_id: str,
        *,
        batch_size: int | None = None,
        max_records: int | None = None,
        restart: bool = False,
        progress_seconds: float = config.INGEST_PROGRESS_SECONDS,
        max_errors_stored: int = config.INGEST_MAX_ERRORS_STORED_PER_FILE,
    ) -> None:
        self.con = con
        self.m = mapping
        self.path = data_dir / mapping.source_file
        self.run_id = run_id
        self.batch_size = batch_size or mapping.batch_size
        self.max_records = max_records
        self.restart = restart
        self.progress_seconds = progress_seconds
        self.max_errors_stored = max_errors_stored
        self.table_sql = db.quote_ident(mapping.table)
        self.ingested_at = datetime.now(timezone.utc).replace(tzinfo=None)

        self.committed: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        self.run_totals: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        self.errors_stored = 0

    # -- manifest -------------------------------------------------------------
    def _load_manifest(self) -> dict[str, Any] | None:
        cur = self.con.execute("SELECT * FROM meta_ingestion_files WHERE source_file = ?", [self.m.source_file])
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def _reset_manifest(self, size: int, mtime_ns: int) -> None:
        with db.transaction(self.con):
            self.con.execute("DELETE FROM meta_ingestion_files WHERE source_file = ?", [self.m.source_file])
            self.con.execute("DELETE FROM meta_ingestion_errors WHERE source_file = ?", [self.m.source_file])
            self.con.execute(
                "INSERT INTO meta_ingestion_files (source_file, target_table, file_size_bytes, file_mtime_ns, "
                "dedupe_strategy, status, last_run_id, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, current_timestamp, current_timestamp)",
                [self.m.source_file, self.m.table, size, mtime_ns, self.m.dedupe_strategy, self.run_id],
            )

    def _decide_start(self, size: int, mtime_ns: int) -> tuple[int, str | None]:
        """Return (start_offset, skip_reason). Resets the manifest when needed."""
        manifest = self._load_manifest()
        reason: str | None = None
        if manifest is None:
            reason = "first ingestion"
        elif self.restart:
            reason = "restart requested"
        elif manifest["target_table"] != self.m.table:
            reason = "target table changed"
        elif manifest["file_size_bytes"] != size or manifest["file_mtime_ns"] != mtime_ns:
            reason = "file changed on disk"
        elif manifest["byte_offset"] > size:
            reason = "stored offset beyond file size"

        if (
            manifest is not None
            and manifest["target_table"] == self.m.table
            and manifest["dedupe_strategy"] != self.m.dedupe_strategy
            and db.table_exists(self.con, self.m.table)
            and db.row_count(self.con, self.m.table) > 0
        ):
            raise ConfigError(
                f"{self.m.source_file}: dedupe_strategy changed for a non-empty table; "
                "re-run with --rebuild for this file."
            )

        if reason is None and manifest["status"] == "completed" and manifest["byte_offset"] == size:
            return size, "already completed (unchanged file)"

        if reason is not None:
            log.info("%s: starting from byte 0 (%s).", self.m.source_file, reason)
            self._reset_manifest(size, mtime_ns)
            return 0, None

        for k in _COUNTERS:
            self.committed[k] = int(manifest[k] or 0)
        log.info("%s: resuming at byte %s (line %s).", self.m.source_file,
                 f"{manifest['byte_offset']:,}", f"{manifest['lines_processed']:,}")
        return int(manifest["byte_offset"]), None

    # -- batch flush ----------------------------------------------------------
    def _flush(self, batch: _Batch, offset: int, status: str) -> None:
        rows = list(batch.rows.values())
        with db.transaction(self.con):
            inserted = 0
            if rows:
                frame = pd.DataFrame({
                    "_record_key": [r[0] for r in rows],
                    "_source_file": [r[1] for r in rows],
                    "_source_line": [r[2] for r in rows],
                    "_source_record_id": [r[3] for r in rows],
                    "payload": [r[4] for r in rows],
                })
                self.con.register("_ingest_batch", frame)
                try:
                    result = self.con.execute(
                        f"INSERT INTO {self.table_sql} "
                        "(_record_key, _source_file, _source_line, _source_record_id, _run_id, _ingested_at, payload) "
                        "SELECT _record_key, _source_file, CAST(_source_line AS BIGINT), "
                        "CAST(_source_record_id AS VARCHAR), ?, CAST(? AS TIMESTAMP), CAST(payload AS JSON) "
                        "FROM _ingest_batch ON CONFLICT DO NOTHING",
                        [self.run_id, self.ingested_at],
                    ).fetchone()
                finally:
                    self.con.unregister("_ingest_batch")
                inserted = int(result[0]) if result and result[0] is not None else 0

            batch.counts["records_inserted"] += inserted
            batch.counts["duplicates_skipped"] += len(rows) - inserted

            room = max(self.max_errors_stored - self.errors_stored, 0)
            errors = batch.errors[:room]
            if errors:
                self.con.executemany(
                    "INSERT INTO meta_ingestion_errors "
                    "(source_file, line_no, byte_offset, error_kind, error_detail, run_id) VALUES (?, ?, ?, ?, ?, ?)",
                    errors,
                )

            new = {k: self.committed[k] + batch.counts[k] for k in _COUNTERS}
            self.con.execute(
                "UPDATE meta_ingestion_files SET "
                + ", ".join(f"{k} = ?" for k in _COUNTERS)
                + ", byte_offset = ?, status = ?, last_run_id = ?, updated_at = current_timestamp, "
                "completed_at = CASE WHEN ? = 'completed' THEN current_timestamp ELSE NULL END "
                "WHERE source_file = ?",
                [*(new[k] for k in _COUNTERS), offset, status, self.run_id, status, self.m.source_file],
            )
        # Only after a successful commit:
        self.committed = new
        self.errors_stored += len(errors)
        for k in _COUNTERS:
            self.run_totals[k] += batch.counts[k]

    # -- main -----------------------------------------------------------------
    def run(self) -> FileResult:
        started = time.perf_counter()
        st = self.path.stat()
        size, mtime_ns = st.st_size, st.st_mtime_ns

        db.create_raw_table(self.con, self.m.table)
        offset, skip = self._decide_start(size, mtime_ns)
        if skip:
            log.info("%s: skipped - %s.", self.m.source_file, skip)
            return FileResult(self.m.source_file, self.m.table, "skipped", note=skip)

        with db.transaction(self.con):
            self.con.execute(
                "DELETE FROM meta_ingestion_errors WHERE source_file = ? AND byte_offset >= ?",
                [self.m.source_file, offset],
            )
            self.con.execute(
                "UPDATE meta_ingestion_files SET status = 'in_progress', last_run_id = ?, "
                "updated_at = current_timestamp WHERE source_file = ?",
                [self.run_id, self.m.source_file],
            )
        self.errors_stored = int(self.con.execute(
            "SELECT count(*) FROM meta_ingestion_errors WHERE source_file = ?", [self.m.source_file]
        ).fetchone()[0])

        line_no = self.committed["lines_processed"]
        id_path = self.m.source_id_field
        use_source_id = self.m.dedupe_strategy == "source_id"
        name = self.m.source_file
        batch = _Batch()
        records_this_run = 0
        stopped_early = False
        last_progress = started
        start_offset = offset

        with self.path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                line_start = offset
                offset += len(raw)
                line_no += 1
                c = batch.counts
                c["lines_processed"] += 1

                try:
                    text = raw.decode("utf-8-sig" if line_start == 0 else "utf-8")
                except UnicodeDecodeError as exc:
                    c["invalid_lines"] += 1
                    batch.errors.append((name, line_no, line_start, "encoding", exc.reason, self.run_id))
                    text = None
                if text is not None:
                    if not text.strip():
                        c["blank_lines"] += 1
                    else:
                        try:
                            obj = json.loads(text)
                        except json.JSONDecodeError as exc:
                            c["invalid_lines"] += 1
                            batch.errors.append((name, line_no, line_start, "json",
                                                 f"{exc.msg} (col {exc.colno})", self.run_id))
                            obj = None
                        except RecursionError:
                            c["invalid_lines"] += 1
                            batch.errors.append((name, line_no, line_start, "json", "nesting too deep", self.run_id))
                            obj = None

                        if obj is not None and not isinstance(obj, dict):
                            c["non_object_lines"] += 1
                            batch.errors.append((name, line_no, line_start, "non_object",
                                                 type(obj).__name__, self.run_id))
                        elif isinstance(obj, dict):
                            c["records_seen"] += 1
                            records_this_run += 1
                            payload, replaced = _canonical_json(obj)
                            c["non_finite_replaced"] += replaced
                            source_id = _id_string(_get_path(obj, id_path)) if id_path else None
                            if use_source_id and source_id is not None:
                                key = _key("id", source_id)
                            else:
                                if use_source_id:
                                    c["missing_source_id"] += 1
                                key = _key("content", payload)
                            if key in batch.rows:
                                c["duplicates_skipped"] += 1
                            else:
                                batch.rows[key] = (key, name, line_no, source_id, payload)

                if len(batch.rows) >= self.batch_size or len(batch.errors) >= self.batch_size:
                    self._flush(batch, offset, "in_progress")
                    batch = _Batch()

                if self.max_records and records_this_run >= self.max_records:
                    stopped_early = offset < size
                    break

                now = time.perf_counter()
                if now - last_progress >= self.progress_seconds:
                    self._log_progress(offset, size, start_offset, now - started, batch)
                    last_progress = now

        status = "partial" if stopped_early else "completed"
        self._flush(batch, offset, status)
        elapsed = time.perf_counter() - started
        self._log_progress(offset, size, start_offset, elapsed, None, final=True)

        if self.m.view and self.m.columns:
            db.create_typed_view(self.con, self.m.view, self.m.table, self.m.columns)
            log.info("%s: view %s refreshed (%d columns).", name, self.m.view, len(self.m.columns))

        return FileResult(
            name, self.m.table, status,
            inserted_this_run=self.run_totals["records_inserted"],
            duplicates_this_run=self.run_totals["duplicates_skipped"],
            invalid_this_run=self.run_totals["invalid_lines"] + self.run_totals["non_object_lines"],
            elapsed_seconds=round(elapsed, 2),
            note="stopped at --max-records" if stopped_early else None,
        )

    def _log_progress(self, offset: int, size: int, start_offset: int, elapsed: float,
                      pending: _Batch | None, final: bool = False) -> None:
        pct = 100.0 * offset / size if size else 100.0
        mbps = (offset - start_offset) / elapsed / 1e6 if elapsed > 0 else 0.0
        inserted = self.run_totals["records_inserted"]
        log.info(
            "%s: %s %5.1f%% | inserted %s | duplicates %s | invalid %s | %.1f MB/s | %.0fs%s",
            self.m.source_file, "done" if final else "....", pct, f"{inserted:,}",
            f"{self.run_totals['duplicates_skipped']:,}",
            f"{self.run_totals['invalid_lines'] + self.run_totals['non_object_lines']:,}",
            mbps, elapsed,
            f" | {len(pending.rows):,} pending" if pending else "",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def rebuild_file(con: duckdb.DuckDBPyConnection, mapping: FileMapping) -> None:
    """Drop a file's raw table, its view and its metadata so it reloads from scratch."""
    with db.transaction(con):
        if mapping.view:
            con.execute(f"DROP VIEW IF EXISTS {db.quote_ident(mapping.view)}")
        db.drop_table(con, mapping.table)
        con.execute("DELETE FROM meta_ingestion_files WHERE source_file = ?", [mapping.source_file])
        con.execute("DELETE FROM meta_ingestion_errors WHERE source_file = ?", [mapping.source_file])
    log.info("%s: rebuilt (table %s dropped).", mapping.source_file, mapping.table)


def run_ingestion(
    mappings: list[FileMapping],
    *,
    data_dir: Path,
    db_path: Path,
    batch_size: int | None = None,
    max_records: int | None = None,
    restart: bool = False,
    rebuild: bool = False,
    debug: bool = False,
) -> int:
    """Ingest the given mappings. Returns a process exit code."""
    selected = [m for m in mappings if m.enabled]
    for m in mappings:
        if not m.enabled:
            log.info("%s: disabled in config - skipped.", m.source_file)
    if not selected:
        log.warning("Nothing to ingest.")
        return 1

    run_id = _new_run_id()
    results: list[FileResult] = []
    failures = 0
    status = "completed"
    with db.db_connection(db_path) as con:
        db.init_metadata(con)
        con.execute(
            "INSERT INTO meta_ingestion_runs (run_id, started_at, status, files_requested) "
            "VALUES (?, current_timestamp, 'running', ?)",
            [run_id, len(selected)],
        )
        log.info("Run %s: %d file(s), database %s", run_id, len(selected), db_path)

        for i, mapping in enumerate(selected, start=1):
            log.info("(%d/%d) %s -> %s  [%.1f MB]", i, len(selected), mapping.source_file, mapping.table,
                     (data_dir / mapping.source_file).stat().st_size / 1e6)
            try:
                if rebuild:
                    rebuild_file(con, mapping)
                results.append(FileIngestor(
                    con, mapping, data_dir, run_id,
                    batch_size=batch_size, max_records=max_records, restart=restart,
                ).run())
            except KeyboardInterrupt:
                log.warning("Interrupted. Committed batches are kept; re-run to resume.")
                status = "interrupted"
                _mark_file(con, mapping.source_file, "in_progress")
                break
            except Exception as exc:
                failures += 1
                log.error("%s: FAILED (%s). Committed batches are kept; re-run to resume.",
                          mapping.source_file, safe_error(exc))
                if isinstance(exc, ConfigError):
                    log.error("  %s", exc)  # our own message: contains no record values
                if debug:
                    # Console only - engine messages may contain values. Do not share.
                    traceback.print_exc()
                _mark_file(con, mapping.source_file, "failed")

        if status != "interrupted" and failures:
            status = "failed" if failures == len(selected) else "completed_with_errors"
        total_inserted = sum(r.inserted_this_run for r in results)
        con.execute(
            "UPDATE meta_ingestion_runs SET finished_at = current_timestamp, status = ?, "
            "records_inserted = ?, files_failed = ? WHERE run_id = ?",
            [status, total_inserted, failures, run_id],
        )

    log.info("Summary for run %s (%s):", run_id, status)
    for r in results:
        log.info("  %-32s %-11s inserted=%12s dup=%10s invalid=%8s %7.1fs%s",
                 r.source_file, r.status, f"{r.inserted_this_run:,}", f"{r.duplicates_this_run:,}",
                 f"{r.invalid_this_run:,}", r.elapsed_seconds, f"  ({r.note})" if r.note else "")
    return 0 if status == "completed" else 1


def _mark_file(con: duckdb.DuckDBPyConnection, source_file: str, status: str) -> None:
    try:
        con.execute(
            "UPDATE meta_ingestion_files SET status = ?, updated_at = current_timestamp WHERE source_file = ?",
            [status, source_file],
        )
    except duckdb.Error as exc:
        log.warning("Could not update status for %s (%s).", source_file, safe_error(exc))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stream local JSONL files into DuckDB (resumable, no duplicates).")
    p.add_argument("--config", type=Path, default=config.INGESTION_CONFIG_PATH)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--files", nargs="+", metavar="NAME", help="Only these file names.")
    p.add_argument("--batch-size", type=int, default=None, help="Override records per transaction.")
    p.add_argument("--max-records", type=int, default=None, help="Stop each file after N records this run.")
    p.add_argument("--restart", action="store_true", help="Rescan files from byte 0 (duplicates still skipped).")
    p.add_argument("--rebuild", action="store_true", help="DROP the selected files' tables and reload them.")
    p.add_argument("--list", action="store_true", help="Print the ingestion plan and exit (reads no data).")
    p.add_argument("--debug", action="store_true", help="Print tracebacks to the console (may include values).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging("ingestion", log_file="ingestion.log")
    data_dir = args.data_dir.resolve()

    if args.batch_size is not None and args.batch_size < 1:
        log.error("--batch-size must be >= 1")
        return 2
    try:
        mappings = load_mappings(args.config.resolve(), data_dir)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2

    if args.files:
        wanted = set(args.files)
        missing = wanted - {m.source_file for m in mappings}
        if missing:
            log.error("Not found in %s: %s", data_dir, sorted(missing))
            return 2
        mappings = [m for m in mappings if m.source_file in wanted]
    if args.rebuild and not args.files:
        log.error("--rebuild requires --files (refusing to drop every table at once).")
        return 2

    if args.list:
        for m in mappings:
            print(f"{m.source_file:<32} -> {m.table:<32} enabled={m.enabled} dedupe={m.dedupe_strategy} "
                  f"id_field={'set' if m.source_id_field else 'none'} batch={args.batch_size or m.batch_size} "
                  f"view={m.view or '-'} columns={len(m.columns)}")
        return 0

    try:
        return run_ingestion(
            mappings, data_dir=data_dir, db_path=args.db.resolve(), batch_size=args.batch_size,
            max_records=args.max_records, restart=args.restart, rebuild=args.rebuild, debug=args.debug,
        )
    except duckdb.IOException as exc:
        log.error("Cannot open database (%s). Is another process using it?", safe_error(exc))
        return 3
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
