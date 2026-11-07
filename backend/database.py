"""
DuckDB database layer for Blast Intelligence AI.

Design
------
The schema has two layers so that nothing about the confidential data has to
be assumed in code:

1. **Raw landing tables** - one per source JSONL file (``raw_<file_stem>`` by
   default). Every record is stored *as-is* in a ``payload JSON`` column, plus
   lineage columns::

       _record_key        VARCHAR PRIMARY KEY  -- dedupe key (content hash or source id hash)
       _source_file       VARCHAR              -- original file name
       _source_line       BIGINT               -- 1-based line number in that file
       _source_record_id  VARCHAR              -- original record id, if one is configured
       _run_id            VARCHAR              -- ingestion run that inserted the row
       _ingested_at       TIMESTAMP

2. **Typed views** - optional, defined in ``ingestion_config.json`` *after*
   local schema inspection. Each configured column is extracted from
   ``payload`` with ``TRY_CAST`` so a bad value becomes NULL instead of failing.

``meta_*`` tables track runs, per-file resume offsets and invalid-line
positions (never line contents).

Every identifier that reaches SQL is validated by ``validate_identifier`` and
every JSON path is validated by ``json_path``; values are always bound as
parameters.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb

import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RAW_TABLE_PREFIX = "raw_"
META_PREFIX = "meta_"

LINEAGE_COLUMNS: tuple[str, ...] = (
    "_record_key",
    "_source_file",
    "_source_line",
    "_source_record_id",
    "_run_id",
    "_ingested_at",
)

# ---------------------------------------------------------------------------
# Identifier / path safety
# ---------------------------------------------------------------------------
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
    """Return ``name`` if it is a safe SQL identifier, else raise ``ValueError``."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid {kind} {name!r}: use letters, digits and '_' (max 63, not starting with a digit).")
    return name


def quote_ident(name: str) -> str:
    """Validate and double-quote an identifier."""
    return f'"{validate_identifier(name)}"'


PathSegment = str | int


def json_path(segments: Sequence[PathSegment]) -> str:
    """Build a DuckDB JSONPath (``$."a"."b"[0]``) from validated segments."""
    if not segments:
        raise ValueError("JSON path must have at least one segment.")
    parts = ["$"]
    for seg in segments:
        if isinstance(seg, bool):
            raise ValueError("JSON path segments must be strings or integers.")
        if isinstance(seg, int):
            if seg < 0:
                raise ValueError("Negative array indexes are not supported.")
            parts.append(f"[{seg}]")
        elif isinstance(seg, str):
            if seg == "" or any(c in seg for c in '"\\\'') or any(ord(c) < 32 for c in seg):
                raise ValueError("JSON key segments may not be empty or contain quotes, backslashes or control chars.")
            parts.append(f'."{seg}"')
        else:
            raise ValueError("JSON path segments must be strings or integers.")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Typed view column specification
# ---------------------------------------------------------------------------
# Config type name -> SQL expression template applied to the extracted text ``{s}``
# or raw JSON ``{j}``.
_TYPE_EXPRESSIONS: dict[str, str] = {
    "VARCHAR": "{s}",
    "BIGINT": "TRY_CAST({s} AS BIGINT)",
    "INTEGER": "TRY_CAST({s} AS INTEGER)",
    "DOUBLE": "TRY_CAST({s} AS DOUBLE)",
    "BOOLEAN": "TRY_CAST({s} AS BOOLEAN)",
    "DATE": "TRY_CAST({s} AS DATE)",
    "TIMESTAMP": "TRY_CAST({s} AS TIMESTAMP)",
    "TIMESTAMPTZ": "TRY_CAST({s} AS TIMESTAMPTZ)",
    "EPOCH_S": "to_timestamp(TRY_CAST({s} AS DOUBLE))",
    "EPOCH_MS": "to_timestamp(TRY_CAST({s} AS DOUBLE) / 1000.0)",
    "JSON": "{j}",
}
ALLOWED_COLUMN_TYPES: frozenset[str] = frozenset(_TYPE_EXPRESSIONS)


@dataclass(frozen=True)
class ColumnSpec:
    """One typed column extracted from ``payload`` into a view."""

    name: str
    path: tuple[PathSegment, ...]
    type: str = "VARCHAR"

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="column name")
        if self.name.startswith("_"):
            raise ValueError(f"Column name {self.name!r} may not start with '_' (reserved for lineage).")
        if self.type.upper() not in ALLOWED_COLUMN_TYPES:
            raise ValueError(f"Column {self.name!r}: type must be one of {sorted(ALLOWED_COLUMN_TYPES)}.")
        json_path(self.path)  # validates

    def sql_expression(self) -> str:
        path = json_path(self.path).replace("'", "''")
        text_expr = f"json_extract_string(payload, '{path}')"
        json_expr = f"json_extract(payload, '{path}')"
        return _TYPE_EXPRESSIONS[self.type.upper()].format(s=text_expr, j=json_expr)


# ---------------------------------------------------------------------------
# Connections and transactions
# ---------------------------------------------------------------------------
_MEMORY_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)

def connect(db_path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with project defaults (local file only)."""
    path = Path(db_path or config.DB_PATH)
    if read_only and not path.exists():
        raise FileNotFoundError(f"Database not found: {path}. Run backend/init_database.py first.")
    path.parent.mkdir(parents=True, exist_ok=True)
    config.DB_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    memory_limit = config.DUCKDB_MEMORY_LIMIT.strip()
    if not _MEMORY_RE.match(memory_limit):
        raise ValueError(f"Invalid DUCKDB_MEMORY_LIMIT {memory_limit!r} (e.g. '4GB').")
    temp_dir = str(config.DB_TEMP_DIR).replace("'", "''")

    con = duckdb.connect(str(path), read_only=read_only)
    try:
        # SET does not accept bound parameters; values are validated above.
        con.execute(f"SET threads = {int(config.DUCKDB_THREADS)}")
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute(f"SET temp_directory = '{temp_dir}'")
        con.execute("SET preserve_insertion_order = false")
        # Keep the engine strictly local.
        con.execute("SET autoinstall_known_extensions = false")
    except duckdb.Error:
        con.close()
        raise
    return con


@contextmanager
def db_connection(db_path: Path | None = None, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """Context manager that always closes the connection."""
    con = connect(db_path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """Run a block in a transaction; roll back on any exception."""
    con.execute("BEGIN TRANSACTION")
    try:
        yield con
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:  # connection may already be unusable
            log.warning("Rollback failed after an error.")
        raise
    else:
        con.execute("COMMIT")


# ---------------------------------------------------------------------------
# Metadata schema
# ---------------------------------------------------------------------------
_META_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta_schema_version (
        version     INTEGER   NOT NULL,
        applied_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_ingestion_runs (
        run_id            VARCHAR   NOT NULL,
        started_at        TIMESTAMP NOT NULL,
        finished_at       TIMESTAMP,
        status            VARCHAR   NOT NULL,   -- running | completed | failed | interrupted
        files_requested   INTEGER,
        records_inserted  BIGINT    DEFAULT 0,
        files_failed      INTEGER   DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_ingestion_files (
        source_file         VARCHAR NOT NULL,   -- original file name
        target_table        VARCHAR NOT NULL,
        file_size_bytes     BIGINT,
        file_mtime_ns       BIGINT,
        byte_offset         BIGINT  NOT NULL DEFAULT 0,   -- resume point (start of next unread line)
        lines_processed     BIGINT  NOT NULL DEFAULT 0,
        records_seen        BIGINT  NOT NULL DEFAULT 0,
        records_inserted    BIGINT  NOT NULL DEFAULT 0,
        duplicates_skipped  BIGINT  NOT NULL DEFAULT 0,
        invalid_lines       BIGINT  NOT NULL DEFAULT 0,
        blank_lines         BIGINT  NOT NULL DEFAULT 0,
        non_object_lines    BIGINT  NOT NULL DEFAULT 0,
        missing_source_id   BIGINT  NOT NULL DEFAULT 0,
        non_finite_replaced BIGINT  NOT NULL DEFAULT 0,
        dedupe_strategy     VARCHAR,
        status              VARCHAR NOT NULL,   -- pending | in_progress | partial | completed | failed
        last_run_id         VARCHAR,
        started_at          TIMESTAMP,
        updated_at          TIMESTAMP,
        completed_at        TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_ingestion_errors (
        source_file   VARCHAR NOT NULL,
        line_no       BIGINT  NOT NULL,
        byte_offset   BIGINT  NOT NULL,
        error_kind    VARCHAR NOT NULL,   -- encoding | json | non_object
        error_detail  VARCHAR,            -- parser message only, never line text
        run_id        VARCHAR,
        recorded_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
)
# Note: meta tables intentionally have no PRIMARY KEY; they are updated in place
# and uniqueness is enforced by the ingestion code (one writer at a time).

META_TABLES: tuple[str, ...] = (
    "meta_schema_version",
    "meta_ingestion_runs",
    "meta_ingestion_files",
    "meta_ingestion_errors",
)


def init_metadata(con: duckdb.DuckDBPyConnection) -> None:
    """Create metadata tables (idempotent) and record the schema version."""
    with transaction(con):
        for ddl in _META_DDL:
            con.execute(ddl)
        current = con.execute("SELECT max(version) FROM meta_schema_version").fetchone()[0]
        if current is None:
            con.execute("INSERT INTO meta_schema_version (version) VALUES (?)", [SCHEMA_VERSION])
        elif current > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{current} is newer than code v{SCHEMA_VERSION}.")


# ---------------------------------------------------------------------------
# Raw tables and views
# ---------------------------------------------------------------------------
def validate_raw_table_name(table: str) -> str:
    validate_identifier(table, kind="table name")
    if table.lower().startswith(META_PREFIX):
        raise ValueError(f"Table name {table!r} may not start with '{META_PREFIX}' (reserved).")
    return table


def create_raw_table(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Create a raw landing table if it does not exist (idempotent)."""
    t = quote_ident(validate_raw_table_name(table))
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {t} (
            _record_key        VARCHAR   PRIMARY KEY,
            _source_file       VARCHAR   NOT NULL,
            _source_line       BIGINT    NOT NULL,
            _source_record_id  VARCHAR,
            _run_id            VARCHAR   NOT NULL,
            _ingested_at       TIMESTAMP NOT NULL,
            payload            JSON      NOT NULL
        )
        """
    )


def drop_table(con: duckdb.DuckDBPyConnection, table: str) -> None:
    con.execute(f"DROP TABLE IF EXISTS {quote_ident(validate_raw_table_name(table))}")


def create_typed_view(
    con: duckdb.DuckDBPyConnection, view: str, table: str, columns: Sequence[ColumnSpec]
) -> None:
    """Create or replace a typed view over a raw table from configured columns."""
    validate_identifier(view, kind="view name")
    if not columns:
        raise ValueError(f"View {view!r} has no columns configured.")
    names = [c.name.lower() for c in columns]
    if len(set(names)) != len(names):
        raise ValueError(f"View {view!r} has duplicate column names.")
    lineage = ", ".join(quote_ident(c) for c in LINEAGE_COLUMNS if c != "_run_id")
    extracted = ",\n    ".join(f"{c.sql_expression()} AS {quote_ident(c.name)}" for c in columns)
    con.execute(
        f"CREATE OR REPLACE VIEW {quote_ident(view)} AS\n"
        f"SELECT {lineage},\n    {extracted}\nFROM {quote_ident(validate_raw_table_name(table))}"
    )


# ---------------------------------------------------------------------------
# Introspection helpers (metadata only)
# ---------------------------------------------------------------------------
def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
        [name],
    ).fetchone()
    return bool(row and row[0])


def list_tables(con: duckdb.DuckDBPyConnection, *, table_type: str = "BASE TABLE") -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = ? ORDER BY table_name",
        [table_type],
    ).fetchall()
    return [r[0] for r in rows]


def list_raw_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [t for t in list_tables(con) if t.startswith(RAW_TABLE_PREFIX)]


def row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()[0])
