"""
Strict read-only query layer for the chat assistant.

A query is executed only if ALL of these pass:

1. Lexical checks - one statement, no comments, starts with SELECT/WITH, no
   forbidden keyword (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, COPY,
   PRAGMA, SET, INSTALL, LOAD, EXPORT ...), no forbidden function
   (read_*, glob, query, getenv, duckdb_*, pragma_*, *_scan ...).
2. Parser check - DuckDB's own parser (``json_serialize_sql``) must accept it
   as exactly one SELECT; every table reference must be an approved typed view
   (or a CTE defined in the query); table functions, schema/catalog-qualified
   names (``information_schema``, ``main.raw_x`` ...) and file paths are rejected.
3. Execution - on a connection opened read-only with external access disabled
   and configuration locked, wrapped in an outer LIMIT, with a timeout that
   interrupts the query.

The chat assistant never executes LLM-written SQL: it builds queries itself
from validated mapping identifiers, and still sends them through this layer.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import duckdb

import config

MAX_SQL_CHARS = 8000

FORBIDDEN_KEYWORDS = frozenset("""
    insert update delete drop alter create attach detach copy export import install load pragma set reset
    call execute prepare deallocate truncate vacuum checkpoint grant revoke begin commit rollback transaction
    use describe show summarize explain merge upsert comment secret force
""".split())
FORBIDDEN_FUNCTIONS = frozenset("""
    glob query query_table getenv sniff_csv current_setting which_secret load_extension json_execute_serialized_sql
    parquet_metadata parquet_schema parquet_file_metadata parquet_kv_metadata iceberg_metadata
""".split())
FORBIDDEN_FUNCTION_PREFIXES = ("read_", "duckdb_", "pragma_", "sqlite_", "postgres_", "mysql_", "iceberg_",
                               "delta_", "parquet_", "from_csv", "from_json", "test_", "checkpoint")
FORBIDDEN_FUNCTION_SUFFIXES = ("_scan",)
ALLOWED_TOP_NODES = {"SELECT_NODE", "SET_OPERATION_NODE", "CTE_NODE", "RECURSIVE_CTE_NODE"}


class UnsafeQueryError(ValueError):
    """The query was rejected. The message names the rule, never data values."""


class QueryTimeout(Exception):
    """The query exceeded its time limit and was interrupted."""


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    tables: frozenset[str]


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: float


# ---------------------------------------------------------------------------
# Lexical layer
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"""
    (?P<string>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<comment>--|/\*)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<space>\s+)
  | (?P<other>.)
""", re.VERBOSE | re.DOTALL)


def _tokens(sql: str) -> list[tuple[str, str]]:
    out = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup
        if kind == "space":
            continue
        if kind == "comment":
            raise UnsafeQueryError("comments are not allowed")
        out.append((kind, m.group()))
    return out


def lexical_check(sql: str) -> str:
    """Return the normalised SQL (trailing ';' removed) or raise ``UnsafeQueryError``."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeQueryError("empty query")
    if len(sql) > MAX_SQL_CHARS:
        raise UnsafeQueryError("query too long")
    if "\x00" in sql:
        raise UnsafeQueryError("invalid characters")
    for quote in ("'", '"'):
        if sql.count(quote) % 2:
            raise UnsafeQueryError("unbalanced quotes")
    toks = _tokens(sql)
    while toks and toks[-1] == ("other", ";"):
        toks.pop()
    if any(t == ("other", ";") for t in toks):
        raise UnsafeQueryError("only one statement is allowed")
    words = [t for k, t in toks if k == "word"]
    if not words or words[0].lower() not in ("select", "with"):
        raise UnsafeQueryError("only SELECT queries are allowed")
    for i, (kind, text) in enumerate(toks):
        if kind != "word":
            continue
        low = text.lower()
        if low in FORBIDDEN_KEYWORDS:
            raise UnsafeQueryError(f"forbidden keyword: {low.upper()}")
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt == ("other", "("):
            if (low in FORBIDDEN_FUNCTIONS or low.startswith(FORBIDDEN_FUNCTION_PREFIXES)
                    or low.endswith(FORBIDDEN_FUNCTION_SUFFIXES)):
                raise UnsafeQueryError(f"forbidden function: {low}")
    cleaned = sql.strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


# ---------------------------------------------------------------------------
# Parser layer
# ---------------------------------------------------------------------------
def _walk(node: Any, tables: list[tuple[str, str, str]], functions: list[str], ctes: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("type")
        if t == "BASE_TABLE":
            tables.append((node.get("catalog_name") or "", node.get("schema_name") or "", node.get("table_name") or ""))
        elif t == "TABLE_FUNCTION":
            fn = node.get("function") or {}
            functions.append(str(fn.get("function_name") or "table function"))
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []) or []:
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    ctes.add(entry["key"].lower())
        for v in node.values():
            _walk(v, tables, functions, ctes)
    elif isinstance(node, list):
        for v in node:
            _walk(v, tables, functions, ctes)


def validate_select(con: duckdb.DuckDBPyConnection, sql: str, approved_views: Iterable[str]) -> ValidatedQuery:
    """Full validation (lexical + DuckDB parser + table allow-list)."""
    cleaned = lexical_check(sql)
    approved = {v.lower() for v in approved_views}
    try:
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [cleaned]).fetchone()[0])
    except (duckdb.Error, ValueError, TypeError):
        raise UnsafeQueryError("query could not be parsed") from None
    if tree.get("error"):
        raise UnsafeQueryError("only a single SELECT statement is allowed")
    stmts = tree.get("statements") or []
    if len(stmts) != 1:
        raise UnsafeQueryError("only one statement is allowed")
    if (stmts[0].get("node") or {}).get("type") not in ALLOWED_TOP_NODES:
        raise UnsafeQueryError("only SELECT queries are allowed")
    tables: list[tuple[str, str, str]] = []
    functions: list[str] = []
    ctes: set[str] = set()
    _walk(stmts[0], tables, functions, ctes)
    if functions:
        raise UnsafeQueryError("table functions are not allowed")
    used = set()
    for catalog, schema, name in tables:
        if catalog or schema:
            raise UnsafeQueryError("qualified table names are not allowed")
        low = name.lower()
        if low in ctes:
            continue
        if low not in approved:
            raise UnsafeQueryError("query references a table or view that is not an approved typed view")
        used.add(low)
    if not used:
        raise UnsafeQueryError("query must read from an approved typed view")
    return ValidatedQuery(cleaned, frozenset(used))


# ---------------------------------------------------------------------------
# Execution layer
# ---------------------------------------------------------------------------
def open_hardened_connection(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Read-only connection with external access disabled and configuration locked."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    config.DB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True, config={
        "threads": int(config.DUCKDB_THREADS),
        "memory_limit": config.DUCKDB_MEMORY_LIMIT,
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
    })
    try:
        con.execute("SET lock_configuration = true")
    except duckdb.Error:
        con.close()
        raise
    return con


class Deadline:
    """Interrupts a connection repeatedly once a deadline passes (for multi-query operations)."""

    def __init__(self, con: duckdb.DuckDBPyConnection, seconds: float) -> None:
        self.con, self.seconds = con, seconds
        self.expired = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        if self._stop.wait(self.seconds):
            return
        self.expired = True
        while not self._stop.is_set():
            try:
                self.con.interrupt()
            except duckdb.Error:
                pass
            self._stop.wait(0.05)

    def __enter__(self) -> "Deadline":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def execute_select(con: duckdb.DuckDBPyConnection, vq: ValidatedQuery, params: list[Any] | None = None, *,
                   row_limit: int | None = None, timeout: float | None = None) -> QueryResult:
    """Execute a validated query with an outer row limit and a timeout."""
    limit = max(1, int(row_limit or config.CHAT_ROW_LIMIT))
    started = time.perf_counter()
    cur = con.cursor()
    try:
        with Deadline(cur, timeout or config.CHAT_QUERY_TIMEOUT_SECONDS) as dl:
            try:
                res = cur.execute(f"SELECT * FROM ({vq.sql}) AS __q LIMIT {limit + 1}", params or [])
                rows = res.fetchall()
                columns = [d[0] for d in res.description]
            except duckdb.Error:
                if dl.expired:
                    raise QueryTimeout("query exceeded its time limit") from None
                raise
    finally:
        cur.close()
    return QueryResult(columns, rows[:limit], len(rows) > limit, round((time.perf_counter() - started) * 1000, 1))
