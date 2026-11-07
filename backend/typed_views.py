"""
Typed DuckDB views built ONLY from the approved schema mapping.

Inputs
------
* ``local_reports/schema_mapping_template.json`` - your approved mapping
* ``local_reports/relationship_validation.json`` - validator results (gates join views)
* ``local_db/blast.duckdb``                     - raw tables from ingestion

What gets created
-----------------
* ``tv_<dataset>`` - one view per approved dataset: lineage columns plus every
  column marked ``"include": true``, the confirmed primary key, and any key
  needed by an approved relationship. Values come from ``payload`` through
  safe conversions (``TRY_CAST`` and guards): an invalid value becomes NULL and
  is counted as a conversion failure.
* ``tj_<id>_<child>_to_<parent>`` - one LEFT JOIN view per relationship that is
  ``"confirmed": true`` in the template AND validated with an accepted status
  (``passed_pending_review``; ``warning`` only with ``--allow-warnings``),
  with a unique target key and row counts that still match the current tables.

Safety
------
* Raw ``raw_*`` and ``meta_*`` tables are never altered or dropped.
* Only views registered in ``meta_typed_views`` (created by this tool) are ever
  replaced or pruned. An unmanaged object with the same name is an error.
* All DDL runs in one transaction: either every view is created or none.
* Re-running is safe; ``CREATE OR REPLACE VIEW`` only touches managed views.
* Only counts are queried. No record values are fetched, printed or written.

Usage
-----
    python backend/typed_views.py --dry-run        # plan + conversion stats, create nothing
    python backend/typed_views.py                  # create / refresh views
    python backend/typed_views.py --strict         # create nothing if any column has an error
    python backend/typed_views.py --prune          # also drop managed views no longer in the mapping
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
import relationship_validator as rv  # noqa: E402
from logging_setup import safe_error  # noqa: E402
from report_io import ReportError, confirm_overwrite, load_json_report, write_json_atomic, write_text_atomic  # noqa: E402

TYPED_VIEWS_VERSION = "1.0"
TEMPLATE_PATH = config.REPORTS_DIR / "schema_mapping_template.json"
VALIDATION_PATH = config.REPORTS_DIR / "relationship_validation.json"
JSON_REPORT = "typed_views_summary.json"
MD_REPORT = "typed_views_summary.md"

DATASET_VIEW_PREFIX = "tv_"
JOIN_VIEW_PREFIX = "tj_"
REGISTRY_TABLE = "meta_typed_views"
LINEAGE = ("_record_key", "_source_file", "_source_line", "_source_record_id")

SUPPORTED_TYPES = ("VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP",
                   "TIMESTAMPTZ", "EPOCH_S", "EPOCH_MS", "JSON")
NO_FAILURE_TYPES = {"VARCHAR", "JSON"}
JOINABLE_TYPES = {"VARCHAR", "BIGINT", "INTEGER"}
_EPOCH_ABS_LIMIT_S = 1e11  # ~ year 5138; beyond this to_timestamp() raises instead of returning NULL


class SetupError(Exception):
    """Configuration/database problem that stops the run. Message contains no data values."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def _fit(name: str, limit: int = 63) -> str:
    """Truncate an identifier to ``limit`` chars, keeping it unique with a short hash."""
    if len(name) <= limit:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[: limit - 9]}_{digest}"


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s or "x"


def column_name_for(field_path: str) -> str:
    """``drill.depth`` -> ``drill_depth`` (safe identifier, never starting with '_' or a digit)."""
    s = _slug(field_path.replace("[]", ""))
    if s[0].isdigit():
        s = f"c_{s}"
    return _fit(s)


def dataset_view_name(dataset: str, table: str | None) -> str:
    base = (table or "").removeprefix(db.RAW_TABLE_PREFIX) or _slug(Path(dataset).stem).lower()
    return _fit(f"{DATASET_VIEW_PREFIX}{base.lower()}")


def join_view_name(rel_id: str, child_view: str, parent_view: str) -> str:
    c = child_view.removeprefix(DATASET_VIEW_PREFIX)
    p = parent_view.removeprefix(DATASET_VIEW_PREFIX)
    return _fit(f"{JOIN_VIEW_PREFIX}{_slug(str(rel_id)).lower()}_{c}_to_{p}")


# ---------------------------------------------------------------------------
# Safe conversion expressions
# ---------------------------------------------------------------------------
def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def raw_text_expr(json_path: str) -> str:
    """Text of the JSON value, with '' treated as missing."""
    return f"NULLIF(json_extract_string(payload, {_sql_str(json_path)}), '')"


def typed_expr(col_type: str, json_path: str, fmt: str | None = None) -> str:
    """SQL expression converting the JSON value safely; invalid values become NULL."""
    return typed_from_text(col_type, raw_text_expr(json_path), fmt,
                           json_expr=f"json_extract(payload, {_sql_str(json_path)})")


def typed_from_text(col_type: str, r: str, fmt: str | None = None, *, json_expr: str | None = None) -> str:
    """Same safe conversion as ``typed_expr``, applied to an already-extracted text expression ``r``.

    Lets measurements extract each JSON value once and reuse it (see ``measure_dataset``).
    """
    d = f"TRY_CAST({r} AS DOUBLE)"
    finite_d = f"(CASE WHEN isfinite({d}) THEN {d} END)"
    t = col_type.upper()
    if t == "VARCHAR":
        return r
    if t == "JSON":
        if json_expr is None:
            raise ValueError("JSON columns need the JSON expression")
        return json_expr
    if t == "DOUBLE":
        return finite_d
    if t in ("BIGINT", "INTEGER"):
        # Refuse silent rounding: '3.5' must fail, '3' and '3.0' succeed.
        return (f"(CASE WHEN {d} IS NULL OR {d} = trunc({d}) THEN TRY_CAST({r} AS {t}) END)")
    if t == "BOOLEAN":
        return f"TRY_CAST({r} AS BOOLEAN)"
    if t == "DATE":
        return f"CAST(try_strptime({r}, {_sql_str(fmt)}) AS DATE)" if fmt else f"TRY_CAST({r} AS DATE)"
    if t == "TIMESTAMP":
        return f"try_strptime({r}, {_sql_str(fmt)})" if fmt else f"TRY_CAST({r} AS TIMESTAMP)"
    if t == "TIMESTAMPTZ":
        return f"TRY_CAST({r} AS TIMESTAMPTZ)"
    if t in ("EPOCH_S", "EPOCH_MS"):
        secs = finite_d if t == "EPOCH_S" else f"({finite_d} / 1000.0)"
        return f"(CASE WHEN abs({secs}) < {_EPOCH_ABS_LIMIT_S:.0f} THEN to_timestamp({secs}) END)"
    raise ValueError(f"unsupported type {col_type}")


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------
@dataclass
class ColumnPlan:
    name: str
    field: str
    type: str
    json_path: str
    origin: str                      # included | primary_key | relationship_key
    fmt: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"               # ok | warning | error
    messages: list[str] = field(default_factory=list)

    @property
    def expr(self) -> str:
        return typed_expr(self.type, self.json_path, self.fmt)


@dataclass
class DatasetPlan:
    dataset: str
    table: str | None
    view: str
    columns: list[ColumnPlan] = field(default_factory=list)
    pk_field: str | None = None
    reason: str = ""                 # why the dataset is in the plan
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    table_rows: int | None = None
    view_rows: int | None = None

    def column_for(self, field_path: str) -> ColumnPlan | None:
        return next((c for c in self.columns if c.field == field_path), None)

    @property
    def usable_columns(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.status != "error"]


@dataclass
class JoinPlan:
    rel_id: str
    child: DatasetPlan
    parent: DatasetPlan
    fk_field: str
    pk_field: str
    validation_status: str
    expected_rows: tuple[Any, Any] = (None, None)   # (child, parent) row counts at validation time
    view: str = ""
    status: str = "planned"          # planned | skipped | created
    reason: str = ""
    parent_aliases: list[tuple[str, str]] = field(default_factory=list)
    rows: int | None = None
    matched_rows: int | None = None


# ---------------------------------------------------------------------------
# Planning (template + validation report only)
# ---------------------------------------------------------------------------
def _column_entry(ds: dict[str, Any], field_path: str) -> dict[str, Any]:
    for c in ds.get("columns", []):
        if isinstance(c, dict) and c.get("field") == field_path:
            return c
    return {}


def _add_column(plan: DatasetPlan, entry: dict[str, Any], field_path: str, origin: str) -> ColumnPlan | None:
    existing = plan.column_for(field_path)
    if existing:
        return existing
    col_type = str(entry.get("type") or entry.get("suggested_type") or "VARCHAR").upper()
    name = entry.get("column_name") or column_name_for(field_path)
    fmt = entry.get("format")
    try:
        if col_type not in SUPPORTED_TYPES:
            raise ValueError(f"type must be one of {', '.join(SUPPORTED_TYPES)}")
        if fmt is not None and (not isinstance(fmt, str) or col_type not in ("DATE", "TIMESTAMP")):
            raise ValueError("'format' is only allowed (as a string) for DATE or TIMESTAMP")
        db.validate_identifier(name, kind="column name")
        if name.startswith("_"):
            raise ValueError("column names may not start with '_' (reserved for lineage)")
        json_path, is_array = rv.profile_path_to_json(field_path)
        if is_array:
            raise ValueError("fields inside arrays cannot be a view column")
    except ValueError as exc:
        plan.errors.append(f"column for field '{field_path}': {exc}")
        return None
    col = ColumnPlan(name=name, field=field_path, type=col_type, json_path=json_path, origin=origin, fmt=fmt)
    plan.columns.append(col)
    return col


def _validation_index(validation: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(validation, dict):
        return {}
    return {str(r.get("id")): r for r in validation.get("relationships", []) if isinstance(r, dict)}


def build_plan(template: dict[str, Any], validation: dict[str, Any] | None, tables: dict[str, str], *,
               allow_warnings: bool) -> tuple[list[DatasetPlan], list[JoinPlan], list[dict[str, str]]]:
    """Return (dataset plans, join plans, skipped relationships) - pure, no database access."""
    datasets_cfg = {k: v for k, v in template["datasets"].items() if isinstance(v, dict) and not k.startswith("_")}
    plans: dict[str, DatasetPlan] = {}

    def plan_for(name: str, reason: str) -> DatasetPlan:
        if name not in plans:
            ds = datasets_cfg[name]
            table = tables.get(name)
            view = ds.get("view_name") or dataset_view_name(name, table)
            plans[name] = DatasetPlan(dataset=name, table=table, view=view, reason=reason)
        return plans[name]

    # 1. Approved datasets: included columns and/or a confirmed primary key.
    for name, ds in datasets_cfg.items():
        included = [c for c in ds.get("columns", []) if isinstance(c, dict) and c.get("include") is True]
        pk = ds.get("primary_key") if isinstance(ds.get("primary_key"), dict) else {}
        pk_field = pk.get("field") if pk.get("confirmed") is True and isinstance(pk.get("field"), str) else None
        if not included and not pk_field:
            continue
        plan = plan_for(name, "included columns" if included else "confirmed primary key")
        if pk_field:
            plan.pk_field = pk_field
            _add_column(plan, _column_entry(ds, pk_field), pk_field, "primary_key")
        for c in included:
            if isinstance(c.get("field"), str):
                _add_column(plan, c, c["field"], "included")
            else:
                plan.errors.append("an included column has no 'field'")

    # 2. Join views: confirmed + validated relationships only.
    vindex = _validation_index(validation)
    accepted = {rv.PASS} | ({rv.WARN} if allow_warnings else set())
    joins: list[JoinPlan] = []
    skipped: list[dict[str, str]] = []
    for rel in template.get("relationships", []):
        if not isinstance(rel, dict) or rel.get("confirmed") is not True:
            continue
        rid = str(rel.get("id"))
        f, t = rel.get("from") or {}, rel.get("to") or {}

        def skip(reason: str) -> None:
            skipped.append({"id": rid, "reason": reason})

        if f.get("dataset") not in datasets_cfg or t.get("dataset") not in datasets_cfg:
            skip("references a dataset that is not in the template")
            continue
        v = vindex.get(rid)
        if v is None:
            skip("not found in relationship_validation.json - run relationship_validator.py")
            continue
        vf, vt = v.get("from") or {}, v.get("to") or {}
        if ((vf.get("dataset"), vf.get("field"), vt.get("dataset"), vt.get("field"))
                != (f.get("dataset"), f.get("field"), t.get("dataset"), t.get("field"))):
            skip("template endpoints changed since validation - re-run relationship_validator.py")
            continue
        if v.get("status") not in accepted:
            skip(f"validation status '{v.get('status')}' is not accepted"
                 + ("" if allow_warnings or v.get("status") != rv.WARN else " (use --allow-warnings)"))
            continue
        if not (v.get("target_side") or {}).get("is_unique"):
            skip("target key was not unique at validation time")
            continue
        if "[]" in str(f.get("field")) or "[]" in str(t.get("field")):
            skip("array-valued keys cannot be joined in a view")
            continue
        child = plan_for(f["dataset"], "relationship key")
        parent = plan_for(t["dataset"], "relationship key")
        _add_column(child, _column_entry(datasets_cfg[f["dataset"]], f["field"]), f["field"], "relationship_key")
        _add_column(parent, _column_entry(datasets_cfg[t["dataset"]], t["field"]), t["field"], "relationship_key")
        joins.append(JoinPlan(
            rel_id=rid, child=child, parent=parent, fk_field=f["field"], pk_field=t["field"],
            validation_status=str(v.get("status")),
            expected_rows=((v.get("source_side") or {}).get("total_rows"),
                           (v.get("target_side") or {}).get("total_rows")),
        ))

    # 3. Duplicate column names within each dataset view.
    for plan in plans.values():
        seen: dict[str, str] = {}
        for c in plan.columns:
            key = c.name.lower()
            if key in seen:
                plan.errors.append(f"duplicate column name '{c.name}' for fields '{seen[key]}' and '{c.field}' "
                                   "(set 'column_name' in the template)")
                c.status = "error"
            else:
                seen[key] = c.field
    return list(plans.values()), joins, skipped


def check_view_names(plans: list[DatasetPlan], joins: list[JoinPlan]) -> list[str]:
    errors, seen = [], {}
    for name, owner in [(p.view, p.dataset) for p in plans] + [(j.view, j.rel_id) for j in joins if j.view]:
        try:
            db.validate_identifier(name, kind="view name")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        low = name.lower()
        if low.startswith((db.RAW_TABLE_PREFIX, db.META_PREFIX)):
            errors.append(f"view name '{name}' uses a reserved prefix")
        if low in seen:
            errors.append(f"duplicate view name '{name}' ({seen[low]} and {owner})")
        seen[low] = owner
    return errors


# ---------------------------------------------------------------------------
# Database measurements (counts only)
# ---------------------------------------------------------------------------
MEASURE_BATCH_COLUMNS = max(1, int(os.environ.get("BLAST_MEASURE_BATCH_COLUMNS", "16")))


def measure_batch_sql(table: str, cols: list[ColumnPlan]) -> str:
    """Counts for a small batch of columns, parsing each row's JSON payload ONCE.

    ``json_extract_string(payload, [path1, ...])`` returns all values of the batch from a
    single parse; presence and conversion checks then reuse those list elements. (The old
    approach re-parsed the payload for every expression of every column in one huge query,
    which exhausted DuckDB's memory on wide tables.)
    """
    paths = ", ".join(_sql_str(c.json_path) for c in cols)
    parts = ["count(*)"]
    for i, c in enumerate(cols, start=1):
        r = f"NULLIF(__v[{i}], '')"
        parts.append(f"count({r})")
        parts.append("0" if c.type in NO_FAILURE_TYPES else
                     f"count(*) FILTER (WHERE {r} IS NOT NULL AND {typed_from_text(c.type, r, c.fmt)} IS NULL)")
    return (f"SELECT {', '.join(parts)} FROM "
            f"(SELECT json_extract_string(payload, [{paths}]) AS __v FROM {db.quote_ident(table)})")


def _measure_batch(con, table: str, cols: list[ColumnPlan], failures: dict[str, str]) -> dict[str, tuple[int, int]]:
    """Measure ``cols``; on a DuckDB error retry with halves down to single columns.

    A column whose own check still fails is reported in ``failures`` - never treated as passing.
    """
    try:
        row = con.execute(measure_batch_sql(table, cols)).fetchone()
    except duckdb.Error as exc:
        if len(cols) == 1:
            failures[cols[0].field] = type(exc).__name__
            return {}
        mid = len(cols) // 2
        out = _measure_batch(con, table, cols[:mid], failures)
        out.update(_measure_batch(con, table, cols[mid:], failures))
        return out
    return {c.field: (int(row[1 + 2 * i]), int(row[2 + 2 * i])) for i, c in enumerate(cols)}


def measure_dataset(con: duckdb.DuckDBPyConnection, plan: DatasetPlan, *, warn_pct: float,
                    batch_size: int | None = None) -> None:
    """Row count plus per-column presence/conversion-failure counts, in bounded batches (counts only)."""
    problem = rv._table_problem(con, plan.table)
    if problem:
        plan.errors.append(problem)
        for c in plan.columns:
            c.status = "error"
        return
    cols = [c for c in plan.columns if c.status != "error"]
    total = int(con.execute(f"SELECT count(*) FROM {db.quote_ident(plan.table)}").fetchone()[0])
    plan.table_rows = total
    size = max(1, batch_size or MEASURE_BATCH_COLUMNS)
    counts: dict[str, tuple[int, int]] = {}
    failures: dict[str, str] = {}
    for start in range(0, len(cols), size):
        counts.update(_measure_batch(con, plan.table, cols[start:start + size], failures))
    for c in cols:
        if c.field in failures:
            c.status = "error"
            c.stats = {"rows": total}
            c.messages.append(f"conversion check could not complete ({failures[c.field]}) - column excluded")
            continue
        present, failed = counts[c.field]
        c.stats = {
            "rows": total,
            "present": present,
            "missing": total - present,
            "missing_pct": rv._pct(total - present, total),
            "converted": present - failed,
            "conversion_failures": failed,
            "conversion_failure_pct": rv._pct(failed, present),
        }
        if total and present == 0:
            c.status = "error"
            c.messages.append("field has no values in the table (missing field or wrong path) - column excluded")
        elif present and failed == present:
            c.status = "error"
            c.messages.append(f"every value failed conversion to {c.type} - column excluded (check its type/format)")
        elif failed and (c.stats["conversion_failure_pct"] or 0) > warn_pct:
            c.status = "warning"
            c.messages.append(f"{failed:,} values ({c.stats['conversion_failure_pct']}%) failed conversion to {c.type}")
        elif failed:
            c.messages.append(f"{failed:,} values failed conversion to {c.type} (NULL in view)")
    if plan.pk_field:
        pk = plan.column_for(plan.pk_field)
        if pk is None or pk.status == "error":
            plan.warnings.append("confirmed primary key column could not be created")


def finalize_joins(joins: list[JoinPlan], con: duckdb.DuckDBPyConnection | None, *, allow_stale: bool) -> None:
    for j in joins:
        fk, pk = j.child.column_for(j.fk_field), j.parent.column_for(j.pk_field)
        expected = j.expected_rows
        if (j.child.table_rows is None or j.parent.table_rows is None
                or not j.child.usable_columns or not j.parent.usable_columns):
            j.status, j.reason = "skipped", "a dataset view for this relationship cannot be built"
        elif fk is None or fk.status == "error" or pk is None or pk.status == "error":
            j.status, j.reason = "skipped", "a join key column could not be created"
        elif fk.type != pk.type:
            j.status, j.reason = "skipped", (f"key types differ ({fk.type} vs {pk.type}) - "
                                             "set the same 'type' for both fields in the template")
        elif fk.type not in JOINABLE_TYPES:
            j.status, j.reason = "skipped", f"key type {fk.type} is not joinable (use VARCHAR, BIGINT or INTEGER)"
        elif (fk.stats.get("conversion_failures") or pk.stats.get("conversion_failures")):
            j.status, j.reason = "skipped", "key columns have conversion failures - joins would silently drop matches"
        elif not allow_stale and con is not None and (expected[0] != j.child.table_rows
                                                      or expected[1] != j.parent.table_rows):
            j.status, j.reason = "skipped", ("row counts changed since validation - re-run relationship_validator.py "
                                             "(or use --allow-stale-validation)")
        if j.status == "skipped":
            continue
        j.view = join_view_name(j.rel_id, j.child.view, j.parent.view)
        prefix = _slug(j.parent.view.removeprefix(DATASET_VIEW_PREFIX)).lower()
        child_names = {c.name.lower() for c in j.child.usable_columns} | set(LINEAGE)
        aliases = [("_record_key", _fit(f"{prefix}__record_key"))]
        aliases += [(c.name, _fit(f"{prefix}__{c.name}")) for c in j.parent.usable_columns]
        clash = [a for _, a in aliases if a.lower() in child_names]
        if clash or len({a.lower() for _, a in aliases}) != len(aliases):
            j.status, j.reason, j.view = "skipped", "joined column names would collide", ""
            continue
        j.parent_aliases = aliases


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
def dataset_view_sql(plan: DatasetPlan) -> str:
    q = db.quote_ident
    cols = [q(c) for c in LINEAGE] + [f"{c.expr} AS {q(c.name)}" for c in plan.usable_columns]
    return (f"CREATE OR REPLACE VIEW {q(plan.view)} AS\nSELECT\n    " + ",\n    ".join(cols)
            + f"\nFROM {q(plan.table)}")


def join_view_sql(j: JoinPlan) -> str:
    q = db.quote_ident
    fk, pk = j.child.column_for(j.fk_field), j.parent.column_for(j.pk_field)
    parent_cols = ",\n    ".join(f"p.{q(src)} AS {q(alias)}" for src, alias in j.parent_aliases)
    return (f"CREATE OR REPLACE VIEW {q(j.view)} AS\nSELECT\n    c.*,\n    {parent_cols}\n"
            f"FROM {q(j.child.view)} AS c\nLEFT JOIN {q(j.parent.view)} AS p ON c.{q(fk.name)} = p.{q(pk.name)}")


def _registry_exists(con: duckdb.DuckDBPyConnection) -> bool:
    return db.table_exists(con, REGISTRY_TABLE)


def managed_views(con: duckdb.DuckDBPyConnection) -> set[str]:
    if not _registry_exists(con):
        return set()
    return {r[0].lower() for r in con.execute(f"SELECT view_name FROM {REGISTRY_TABLE}").fetchall()}


def name_conflicts(con: duckdb.DuckDBPyConnection, names: list[str], *, replace_unmanaged: bool) -> list[str]:
    managed = managed_views(con)
    objects = {r[0].lower(): r[1] for r in con.execute(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
    errors = []
    for n in names:
        kind = objects.get(n.lower())
        if kind is None or n.lower() in managed:
            continue
        if kind == "BASE TABLE":
            errors.append(f"'{n}' already exists as a TABLE - refusing to replace it")
        elif not replace_unmanaged:
            errors.append(f"view '{n}' exists but was not created by typed_views.py "
                          "(rename it, or use --replace-unmanaged)")
    return errors


def create_views(con: duckdb.DuckDBPyConnection, plans: list[DatasetPlan], joins: list[JoinPlan], run_id: str,
                 *, prune: bool) -> list[str]:
    """Create all views in one transaction. Returns the names of pruned views."""
    pruned: list[str] = []
    with db.transaction(con):
        con.execute(f"""CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
            view_name VARCHAR NOT NULL, kind VARCHAR NOT NULL, dataset VARCHAR, relationship_id VARCHAR,
            definition_sha VARCHAR, run_id VARCHAR, updated_at TIMESTAMP DEFAULT current_timestamp)""")
        entries = []
        for p in plans:
            sql = dataset_view_sql(p)
            con.execute(sql)
            entries.append((p.view, "dataset", p.dataset, None, sql))
        for j in joins:
            sql = join_view_sql(j)
            con.execute(sql)
            entries.append((j.view, "join", j.child.dataset, j.rel_id, sql))
        wanted = {e[0].lower() for e in entries}
        if prune:
            views = {r[0].lower(): r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'VIEW'").fetchall()}
            for name in sorted(managed_views(con) - wanted):
                if name in views:  # only ever DROP VIEW, and only managed ones
                    con.execute(f"DROP VIEW {db.quote_ident(views[name])}")
                    pruned.append(views[name])
                con.execute(f"DELETE FROM {REGISTRY_TABLE} WHERE lower(view_name) = ?", [name])
        for name, kind, dataset, rel_id, sql in entries:
            con.execute(f"DELETE FROM {REGISTRY_TABLE} WHERE lower(view_name) = ?", [name.lower()])
            con.execute(f"INSERT INTO {REGISTRY_TABLE} (view_name, kind, dataset, relationship_id, definition_sha, "
                        "run_id) VALUES (?, ?, ?, ?, ?, ?)",
                        [name, kind, dataset, rel_id, hashlib.sha256(sql.encode()).hexdigest()[:16], run_id])
    return pruned


def post_check(con: duckdb.DuckDBPyConnection, plans: list[DatasetPlan], joins: list[JoinPlan]) -> None:
    q = db.quote_ident
    for p in plans:
        p.view_rows = int(con.execute(f"SELECT count(*) FROM {q(p.view)}").fetchone()[0])
        if p.view_rows != p.table_rows:
            p.warnings.append(f"view has {p.view_rows:,} rows but table has {p.table_rows:,}")
    for j in joins:
        pk_alias = next(a for src, a in j.parent_aliases if src == "_record_key")
        rows, matched = con.execute(
            f"SELECT count(*), count({q(pk_alias)}) FROM {q(j.view)}").fetchone()
        j.rows, j.matched_rows, j.status = int(rows), int(matched), "created"
        if j.rows != j.child.table_rows:
            j.reason = f"join view has {j.rows:,} rows but child table has {j.child.table_rows:,} (fan-out)"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def build_report(plans, joins, skipped, global_errors, *, dry_run: bool, created: bool, pruned: list[str],
                 db_path: Path, template_path: Path, validation_path: Path, opts: dict[str, Any]) -> dict[str, Any]:
    def col(c: ColumnPlan) -> dict[str, Any]:
        return {"column": c.name, "field": c.field, "type": c.type, "format": c.fmt, "origin": c.origin,
                "status": c.status, **c.stats, "messages": c.messages}
    return {
        "typed_views_version": TYPED_VIEWS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": "Row counts and conversion-failure counts only. No record values.",
        "mode": "dry-run" if dry_run else "create",
        "views_created": created,
        "database": str(db_path), "template": str(template_path), "validation_report": str(validation_path),
        "options": opts,
        "errors": global_errors,
        "summary": {
            "dataset_views": len(plans),
            "join_views": sum(1 for j in joins if j.status in ("planned", "created")),
            "relationships_skipped": len(skipped) + sum(1 for j in joins if j.status == "skipped"),
            "columns": sum(len(p.columns) for p in plans),
            "columns_excluded": sum(1 for p in plans for c in p.columns if c.status == "error"),
            "columns_with_warnings": sum(1 for p in plans for c in p.columns if c.status == "warning"),
            "pruned_views": pruned,
        },
        "dataset_views": [{
            "view": p.view, "dataset": p.dataset, "source_table": p.table, "reason": p.reason,
            "primary_key_field": p.pk_field, "table_rows": p.table_rows, "view_rows": p.view_rows,
            "errors": p.errors, "warnings": p.warnings, "columns": [col(c) for c in p.columns],
        } for p in plans],
        "join_views": [{
            "view": j.view or None, "relationship_id": j.rel_id, "status": j.status, "note": j.reason or None,
            "child": {"dataset": j.child.dataset, "field": j.fk_field, "view": j.child.view},
            "parent": {"dataset": j.parent.dataset, "field": j.pk_field, "view": j.parent.view},
            "validation_status": j.validation_status, "rows": j.rows, "matched_rows": j.matched_rows,
            "unmatched_rows": (j.rows - j.matched_rows) if j.rows is not None and j.matched_rows is not None else None,
        } for j in joins],
        "relationships_not_joined": skipped,
    }


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v).replace("|", "/").replace("\n", " ")


def render_markdown(r: dict[str, Any]) -> str:
    s = r["summary"]
    out = ["# Typed views summary", "",
           f"_Generated {r['generated_at_utc']} ({r['mode']}). Counts only - no record values._", "",
           f"Dataset views: **{s['dataset_views']}** - join views: **{s['join_views']}** - "
           f"columns: {s['columns']} (excluded {s['columns_excluded']}, warnings {s['columns_with_warnings']})", ""]
    if r["errors"]:
        out += ["## Errors", ""] + [f"- {_cell(e)}" for e in r["errors"]] + [""]
    for d in r["dataset_views"]:
        out += [f"## `{d['view']}` - {_cell(d['dataset'])}", "",
                f"Source table `{_cell(d['source_table'])}` - table rows {_cell(d['table_rows'])} - "
                f"view rows {_cell(d['view_rows'])} - primary key {_cell(d['primary_key_field'])}", "",
                "| column | field | type | origin | status | present | missing | conversion failures | failure % |",
                "|---|---|---|---|---|---|---|---|---|"]
        for c in d["columns"]:
            out.append("| " + " | ".join(_cell(x) for x in (
                c["column"], c["field"], c["type"] + (f" ({c['format']})" if c.get("format") else ""), c["origin"],
                c["status"], c.get("present"), c.get("missing"), c.get("conversion_failures"),
                c.get("conversion_failure_pct"))) + " |")
        notes = d["errors"] + d["warnings"] + [f"{c['column']}: {m}" for c in d["columns"] for m in c["messages"]]
        out += [""] + [f"- {_cell(n)}" for n in notes] + ([""] if notes else [])
    out += ["## Join views", "", "| relationship | view | status | rows | matched | unmatched | note |",
            "|---|---|---|---|---|---|---|"]
    for j in r["join_views"]:
        out.append("| " + " | ".join(_cell(x) for x in (
            j["relationship_id"], j["view"], j["status"], j["rows"], j["matched_rows"], j["unmatched_rows"],
            j["note"])) + " |")
    for sk in r["relationships_not_joined"]:
        out.append(f"| {_cell(sk['id'])} | - | not joined | - | - | - | {_cell(sk['reason'])} |")
    return "\n".join(out) + "\n"


def print_summary(r: dict[str, Any]) -> None:
    print(f"\nMode: {r['mode']}   views created: {r['views_created']}")
    for e in r["errors"]:
        print(f"ERROR: {e}")
    print(f"\n{'view':<40} {'rows':>12} {'cols':>5} {'excluded':>9} {'failures':>10}")
    for d in r["dataset_views"]:
        fails = sum(c.get("conversion_failures") or 0 for c in d["columns"])
        excl = sum(1 for c in d["columns"] if c["status"] == "error")
        rows = d["view_rows"] if d["view_rows"] is not None else d["table_rows"]
        print(f"{d['view']:<40} {_cell(rows):>12} {len(d['columns']) - excl:>5} {excl:>9} {fails:>10,}")
        for e in d["errors"]:
            print(f"    ERROR: {e}")
    for j in r["join_views"]:
        print(f"{str(j['view'] or '(' + j['relationship_id'] + ')'):<40} {_cell(j['rows']):>12}  "
              f"{j['status']}{'  - ' + j['note'] if j['note'] else ''}")
    for sk in r["relationships_not_joined"]:
        print(f"  not joined {sk['id']}: {sk['reason']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create typed DuckDB views from the approved mapping (counts only).")
    p.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    p.add_argument("--validation", type=Path, default=VALIDATION_PATH)
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--output-dir", type=Path, default=config.REPORTS_DIR)
    p.add_argument("--dry-run", action="store_true", help="Plan and measure only; create nothing (read-only).")
    p.add_argument("--strict", action="store_true", help="Create nothing if any column or dataset has an error.")
    p.add_argument("--allow-warnings", action="store_true", help="Also join relationships validated as 'warning'.")
    p.add_argument("--allow-stale-validation", action="store_true",
                   help="Join even if table row counts changed since validation.")
    p.add_argument("--warn-failure-pct", type=float, default=1.0,
                   help="Conversion-failure %% above which a column gets a warning (default 1).")
    p.add_argument("--prune", action="store_true", help="Drop managed views that are no longer in the mapping.")
    p.add_argument("--replace-unmanaged", action="store_true",
                   help="Allow replacing same-named views not created by this tool (never tables).")
    p.add_argument("--force", action="store_true", help="Overwrite existing reports without asking (.bak kept).")
    p.add_argument("--debug", action="store_true", help="Print tracebacks to the console (may include values).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    template_path, validation_path = args.template.resolve(), args.validation.resolve()
    db_path, out_dir = args.db.resolve(), args.output_dir.resolve()
    global_errors: list[str] = []
    created, pruned = False, []

    try:
        template = rv.load_template(template_path)
        try:
            validation = load_json_report(validation_path, what="Validation report")
        except ReportError as exc:
            validation = None
            global_errors.append(f"{exc} - no join views can be created.")
        if not db_path.exists():
            raise SetupError(f"Database not found: {db_path}. Run init_database.py and ingestion.py first.")

        with db.db_connection(db_path, read_only=args.dry_run) as con:
            tables = rv.resolve_tables(con, template)
            plans, joins, skipped = build_plan(template, validation, tables, allow_warnings=args.allow_warnings)
            if not plans:
                raise SetupError('No approved datasets: mark columns "include": true or confirm a primary key.')
            for p in plans:
                measure_dataset(con, p, warn_pct=args.warn_failure_pct)
            buildable = [p for p in plans if p.table_rows is not None and p.usable_columns]
            finalize_joins(joins, con, allow_stale=args.allow_stale_validation)
            for p in plans:
                if p not in buildable and p.table_rows is not None:
                    p.errors.append("no usable columns - view not created")
            ready_joins = [j for j in joins if j.status == "planned"]
            naming_errors = check_view_names(buildable, ready_joins)
            naming_errors += name_conflicts(con, [p.view for p in buildable] + [j.view for j in ready_joins],
                                            replace_unmanaged=args.replace_unmanaged)
            global_errors += naming_errors
            has_column_errors = any(p.errors or any(c.status == "error" for c in p.columns) for p in plans)

            if args.dry_run:
                pass
            elif naming_errors:
                global_errors.append("Naming conflicts found - no views were created.")
            elif args.strict and has_column_errors:
                global_errors.append("--strict: errors found, no views were created.")
            else:
                run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                pruned = create_views(con, buildable, ready_joins, run_id, prune=args.prune)
                created = True
                post_check(con, buildable, ready_joins)
    except (SetupError, rv.ValidationSetupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except duckdb.IOException as exc:
        print(f"ERROR: cannot open database ({safe_error(exc)}). Is another process using it?", file=sys.stderr)
        return 3
    except duckdb.Error as exc:
        if args.debug:
            traceback.print_exc()
        print(f"ERROR: database error ({safe_error(exc)}); no views were changed. Re-run with --debug locally.",
              file=sys.stderr)
        return 1

    report = build_report(plans, joins, skipped, global_errors, dry_run=args.dry_run, created=created,
                          pruned=pruned, db_path=db_path, template_path=template_path,
                          validation_path=validation_path,
                          opts={"allow_warnings": args.allow_warnings, "strict": args.strict,
                                "allow_stale_validation": args.allow_stale_validation,
                                "warn_failure_pct": args.warn_failure_pct, "prune": args.prune})
    print_summary(report)
    paths = [out_dir / JSON_REPORT, out_dir / MD_REPORT]
    if confirm_overwrite(paths, force=args.force):
        write_json_atomic(paths[0], report)
        write_text_atomic(paths[1], render_markdown(report))
        print("Wrote:\n  " + "\n  ".join(str(p) for p in paths))
    else:
        print("Reports not written.")
    failed = global_errors or has_column_errors
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
