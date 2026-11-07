"""
Read-only health check for the local DuckDB database.

Reports counts and statuses only - never record contents:

* schema version and presence of ``meta_*`` tables
* per raw table: row count, duplicate-key check, lineage nulls
* per source file: ingestion status, resume offset vs. current file size
  (filesystem ``stat`` only), invalid-line counts by kind
* per typed view: row count and non-null fill rate per configured column
* recent ingestion runs

Usage
-----
    python backend/check_database.py
    python backend/check_database.py --json      # also writes local_reports/db_health.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
from logging_setup import safe_error, setup_logging  # noqa: E402


class Health:
    """Collects check results and issues."""

    def __init__(self) -> None:
        self.issues: list[str] = []
        self.warnings: list[str] = []
        self.report: dict[str, Any] = {}

    def issue(self, msg: str) -> None:
        self.issues.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _check_meta(con: duckdb.DuckDBPyConnection, h: Health) -> bool:
    missing = [t for t in db.META_TABLES if not db.table_exists(con, t)]
    if missing:
        h.issue(f"Missing metadata tables: {missing}. Run backend/init_database.py.")
        return False
    version = con.execute("SELECT max(version) FROM meta_schema_version").fetchone()[0]
    h.report["schema_version"] = version
    if version != db.SCHEMA_VERSION:
        h.warn(f"Schema version {version} differs from code version {db.SCHEMA_VERSION}.")
    return True


def _check_raw_tables(con: duckdb.DuckDBPyConnection, h: Health) -> None:
    out = []
    for t in db.list_raw_tables(con):
        q = db.quote_ident(t)
        total, distinct, null_lineage, files = con.execute(
            f"SELECT count(*), count(DISTINCT _record_key), "
            f"count(*) FILTER (WHERE _source_file IS NULL OR _source_line IS NULL OR payload IS NULL), "
            f"count(DISTINCT _source_file) FROM {q}"
        ).fetchone()
        with_id = con.execute(f"SELECT count(_source_record_id) FROM {q}").fetchone()[0]
        out.append({"table": t, "rows": total, "distinct_keys": distinct, "rows_with_source_id": with_id,
                    "source_files": files, "rows_missing_lineage": null_lineage})
        if total != distinct:
            h.issue(f"{t}: {total - distinct} duplicate _record_key values.")
        if null_lineage:
            h.issue(f"{t}: {null_lineage} rows missing lineage/payload.")
        if files > 1:
            h.warn(f"{t}: rows come from {files} different source files.")
    h.report["raw_tables"] = out


def _check_files(con: duckdb.DuckDBPyConnection, h: Health, data_dir: Path) -> None:
    manifest = _rows(con, "SELECT * FROM meta_ingestion_files ORDER BY source_file")
    errors = _rows(con, "SELECT source_file, error_kind, count(*) AS n FROM meta_ingestion_errors "
                        "GROUP BY ALL ORDER BY ALL")
    by_file: dict[str, dict[str, int]] = {}
    for e in errors:
        by_file.setdefault(e["source_file"], {})[e["error_kind"]] = e["n"]

    seen = set()
    out = []
    for m in manifest:
        name = m["source_file"]
        if name in seen:
            h.issue(f"{name}: multiple manifest rows.")
        seen.add(name)
        path = data_dir / name
        current_size = path.stat().st_size if path.is_file() else None
        changed = current_size is not None and (
            current_size != m["file_size_bytes"] or path.stat().st_mtime_ns != m["file_mtime_ns"]
        )
        pct = round(100 * m["byte_offset"] / current_size, 2) if current_size else None
        row = {
            "source_file": name, "target_table": m["target_table"], "status": m["status"],
            "progress_pct": pct, "lines_processed": m["lines_processed"], "records_seen": m["records_seen"],
            "records_inserted": m["records_inserted"], "duplicates_skipped": m["duplicates_skipped"],
            "invalid_lines": m["invalid_lines"], "non_object_lines": m["non_object_lines"],
            "blank_lines": m["blank_lines"], "missing_source_id": m["missing_source_id"],
            "non_finite_replaced": m["non_finite_replaced"], "dedupe_strategy": m["dedupe_strategy"],
            "stored_error_positions": by_file.get(name, {}), "file_changed_since_ingest": changed,
            "updated_at": str(m["updated_at"]) if m["updated_at"] else None,
        }
        out.append(row)
        if current_size is None:
            h.warn(f"{name}: source file no longer present in {data_dir}.")
        if changed:
            h.warn(f"{name}: file changed since ingestion - re-run ingestion.")
        if m["status"] != "completed":
            h.warn(f"{name}: status '{m['status']}' ({pct}% read) - re-run ingestion to resume.")
        if db.table_exists(con, m["target_table"]):
            actual = con.execute(
                f"SELECT count(*) FROM {db.quote_ident(m['target_table'])} WHERE _source_file = ?", [name]
            ).fetchone()[0]
            # actual > manifest is normal after --restart (counters reset, rows kept).
            if actual < m["records_inserted"]:
                h.issue(f"{name}: manifest says {m['records_inserted']:,} inserted, table has only {actual:,}.")
            row["rows_in_table"] = actual
        else:
            h.issue(f"{name}: target table {m['target_table']} is missing.")

    if data_dir.is_dir():
        for p in sorted(data_dir.glob(config.DATA_FILE_GLOB)):
            if p.name not in seen:
                h.warn(f"{p.name}: not ingested yet.")
    h.report["files"] = out


def _check_views(con: duckdb.DuckDBPyConnection, h: Health) -> None:
    out = []
    for v in db.list_tables(con, table_type="VIEW"):
        q = db.quote_ident(v)
        try:
            cols = [r[0] for r in con.execute(f"DESCRIBE {q}").fetchall()]
            data_cols = [c for c in cols if not c.startswith("_")]
            exprs = ", ".join(["count(*)"] + [f"count({db.quote_ident(c)})" for c in data_cols])
            counts = con.execute(f"SELECT {exprs} FROM {q}").fetchone()
        except (duckdb.Error, ValueError) as exc:
            h.issue(f"View {v} is broken ({safe_error(exc)}).")
            continue
        total = counts[0]
        fill = {c: (round(100 * n / total, 2) if total else None) for c, n in zip(data_cols, counts[1:])}
        out.append({"view": v, "rows": total, "non_null_pct": fill})
        for c, pct in fill.items():
            if total and pct == 0:
                h.warn(f"View {v}.{c} is always NULL - check its path/type in ingestion_config.json.")
    h.report["views"] = out


def _recent_runs(con: duckdb.DuckDBPyConnection, h: Health) -> None:
    runs = _rows(con, "SELECT run_id, started_at, finished_at, status, files_requested, records_inserted, "
                      "files_failed FROM meta_ingestion_runs ORDER BY started_at DESC LIMIT 5")
    for r in runs:
        r["started_at"] = str(r["started_at"])
        r["finished_at"] = str(r["finished_at"]) if r["finished_at"] else None
    if runs and runs[0]["status"] == "running":
        h.warn("Latest run is still 'running' (or was killed without cleanup).")
    h.report["recent_runs"] = runs


def _print(h: Health, db_path: Path) -> None:
    r = h.report
    print(f"\nDatabase: {db_path}  ({r.get('db_size_mb', 0):.1f} MB, schema v{r.get('schema_version')})")
    if r.get("raw_tables"):
        print("\nRaw tables")
        for t in r["raw_tables"]:
            print(f"  {t['table']:<34} rows={t['rows']:>12,}  with_source_id={t['rows_with_source_id']:>12,}")
    if r.get("files"):
        print("\nSource files")
        for f in r["files"]:
            print(f"  {f['source_file']:<32} {f['status']:<11} {f['progress_pct'] or 0:>6.1f}%  "
                  f"inserted={f['records_inserted']:>12,}  dup={f['duplicates_skipped']:>10,}  "
                  f"invalid={f['invalid_lines'] + f['non_object_lines']:>8,}")
    if r.get("views"):
        print("\nViews")
        for v in r["views"]:
            low = [c for c, p in v["non_null_pct"].items() if p is not None and p < 50]
            print(f"  {v['view']:<34} rows={v['rows']:>12,}  columns={len(v['non_null_pct'])}"
                  f"  <50% filled: {len(low)}")
    for w in h.warnings:
        print(f"WARNING: {w}")
    for i in h.issues:
        print(f"ISSUE:   {i}")
    print("\nHEALTHY" if not h.issues else f"\n{len(h.issues)} issue(s) found")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only DuckDB health check (counts only).")
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--json", action="store_true", help="Also write local_reports/db_health.json")
    args = p.parse_args(argv)
    log = setup_logging("check_database")
    db_path = args.db.resolve()

    h = Health()
    try:
        with db.db_connection(db_path, read_only=True) as con:
            h.report["db_size_mb"] = db_path.stat().st_size / 1e6
            h.report["duckdb_version"] = duckdb.__version__
            if _check_meta(con, h):
                _check_raw_tables(con, h)
                _check_files(con, h, args.data_dir.resolve())
                _check_views(con, h)
                _recent_runs(con, h)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except duckdb.IOException as exc:
        log.error("Cannot open database read-only (%s). Is ingestion running?", safe_error(exc))
        return 3
    except duckdb.Error as exc:
        log.error("Health check failed (%s).", safe_error(exc))
        return 1

    _print(h, db_path)
    if args.json:
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = config.REPORTS_DIR / "db_health.json"
        payload = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "issues": h.issues, "warnings": h.warnings, **h.report}
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {out}")
    return 0 if not h.issues else 1


if __name__ == "__main__":
    sys.exit(main())
