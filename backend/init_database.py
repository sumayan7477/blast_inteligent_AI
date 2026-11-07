"""
Initialise the local DuckDB database (idempotent). Reads NO record data.

* creates ``local_db/`` and the other Git-ignored directories
* creates the ``meta_*`` tables
* creates one empty raw table per discovered JSONL file (by file name only)
* creates typed views for mappings that define ``columns``
* optionally writes a mapping skeleton ``backend/ingestion_config.json``

Usage
-----
    python backend/init_database.py --write-config   # first time
    python backend/init_database.py                  # after editing ingestion_config.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
from ingestion import ConfigError, load_mappings, write_config_skeleton  # noqa: E402
from logging_setup import safe_error, setup_logging  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create the local DuckDB schema (no data is read).")
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--config", type=Path, default=config.INGESTION_CONFIG_PATH)
    p.add_argument("--write-config", action="store_true",
                   help="Write a mapping skeleton (file names only) if the config does not exist.")
    p.add_argument("--overwrite-config", action="store_true", help="With --write-config: replace an existing config.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log = setup_logging("init_database", log_file="init_database.log")
    config.ensure_local_dirs()
    data_dir, cfg_path, db_path = args.data_dir.resolve(), args.config.resolve(), args.db.resolve()

    if args.write_config:
        try:
            if write_config_skeleton(cfg_path, data_dir, overwrite=args.overwrite_config):
                log.info("Wrote mapping skeleton %s (git-ignored).", cfg_path)
            else:
                log.info("Config %s already exists - left unchanged (use --overwrite-config).", cfg_path)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return 2

    try:
        mappings = load_mappings(cfg_path, data_dir)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2

    try:
        with db.db_connection(db_path) as con:
            db.init_metadata(con)
            with db.transaction(con):
                for m in mappings:
                    if not m.enabled:
                        log.info("%-32s disabled - no table created.", m.source_file)
                        continue
                    db.create_raw_table(con, m.table)
                    if m.view:
                        db.create_typed_view(con, m.view, m.table, m.columns)
                    log.info("%-32s -> table %s%s", m.source_file, m.table,
                             f", view {m.view} ({len(m.columns)} cols)" if m.view else "")
            tables = db.list_tables(con)
            views = db.list_tables(con, table_type="VIEW")
    except duckdb.IOException as exc:
        log.error("Cannot open %s (%s). Is another process using it?", db_path, safe_error(exc))
        return 3
    except (duckdb.Error, ValueError) as exc:
        log.error("Initialisation failed (%s).", safe_error(exc))
        return 1

    log.info("Database ready: %s | %d tables, %d views, schema v%d", db_path, len(tables), len(views),
             db.SCHEMA_VERSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
