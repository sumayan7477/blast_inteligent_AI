"""
Validate confirmed schema relationships against the local DuckDB database.

Input : ``local_reports/schema_mapping_template.json`` (your confirmations)
        ``local_db/blast.duckdb``                      (opened READ-ONLY)
Output: ``local_reports/relationship_validation.json``
        ``local_reports/relationship_validation.md``

For each relationship ``from`` (referencing / child side) -> ``to``
(referenced / parent side) it measures, with exact SQL aggregates:

* row counts, null keys, distinct keys, duplicated keys on both sides
* whether the referenced key is unique
* matched / unmatched key values (rows and distinct), overlap percentages
* orphan records (child rows with at least one unmatched key)
* parent coverage (referenced parent keys / all parent keys)
* observed cardinality vs. the cardinality declared in the template

It also checks every confirmed primary key for nulls and duplicates.

This tool NEVER changes the template and NEVER confirms anything: every
result carries ``human_review_required: true``. Only counts and percentages
are produced - key values are never selected into Python, printed or
written.

Usage
-----
    python backend/relationship_validator.py                       # all confirmed relationships
    python backend/relationship_validator.py --relationship R003   # just one (repeatable)
    python backend/relationship_validator.py --include-unconfirmed # also gather evidence for candidates
    python backend/relationship_validator.py --list                # list relationships in the template
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
from logging_setup import safe_error  # noqa: E402
from report_io import ReportError, confirm_overwrite, load_json_report, write_json_atomic, write_text_atomic  # noqa: E402

VALIDATOR_VERSION = "1.0"
TEMPLATE_PATH = config.REPORTS_DIR / "schema_mapping_template.json"
JSON_REPORT = "relationship_validation.json"
MD_REPORT = "relationship_validation.md"

PASS = "passed_pending_review"
WARN = "warning"
FAIL = "failed"
ERROR = "error"
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2, ERROR: 3}


class ValidationSetupError(Exception):
    """Template/database problem that prevents validation. Message has no data values."""


# ---------------------------------------------------------------------------
# Options and endpoints
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Options:
    warn_threshold_pct: float = 90.0  # overlap below this (with orphans) -> failed
    trim_keys: bool = False
    case_insensitive: bool = False


@dataclass(frozen=True)
class Endpoint:
    dataset: str
    field: str
    table: str
    json_path: str
    is_array: bool


_SEGMENT_RE = re.compile(r"^(?P<name>.*?)(?P<arrays>(?:\[\])*)$")


def profile_path_to_json(path: str) -> tuple[str, bool]:
    """Convert a profile path (``a.b``, ``a[].b``) into a DuckDB JSONPath and an is-array flag."""
    if not isinstance(path, str) or not path:
        raise ValueError("field path must be a non-empty string")
    parts, is_array = ["$"], False
    for seg in path.split("."):
        m = _SEGMENT_RE.match(seg)
        name, arrays = m.group("name"), m.group("arrays")
        if not name and not arrays:
            raise ValueError("field path has an empty segment")
        if name:
            if any(c in name for c in "\"'\\[]") or any(ord(c) < 32 for c in name):
                raise ValueError("field path contains unsupported characters")
            parts.append(f'."{name}"')
        if arrays:
            is_array = True
            parts.append("[*]" * (len(arrays) // 2))
    return "".join(parts), is_array


def _normalise(expr: str, opts: Options) -> str:
    if opts.trim_keys:
        expr = f"trim({expr})"
    if opts.case_insensitive:
        expr = f"lower({expr})"
    return f"NULLIF({expr}, '')"


def extraction_sql(ep: Endpoint, opts: Options) -> str:
    """SQL yielding one row per key value: (rid, k). Rows without a key yield k = NULL."""
    table = db.quote_ident(ep.table)
    path = ep.json_path.replace("'", "''")
    if not ep.is_array:
        key_expr = _normalise(f"json_extract_string(payload, '{path}')", opts)
        return f"SELECT _record_key AS rid, {key_expr} AS k FROM {table}"
    return (
        f"SELECT rid, {_normalise('k0', opts)} AS k FROM ("
        f"SELECT rid, unnest(CASE WHEN l IS NULL OR len(l) = 0 THEN [NULL::VARCHAR] ELSE l END) AS k0 FROM ("
        f"SELECT _record_key AS rid, json_extract_string(payload, '{path}') AS l FROM {table}))"
    )


# ---------------------------------------------------------------------------
# Template + table resolution
# ---------------------------------------------------------------------------
def load_template(path: Path) -> dict[str, Any]:
    try:
        raw = load_json_report(path, what="Mapping template")
    except ReportError as exc:
        raise ValidationSetupError(str(exc)) from None
    if not isinstance(raw, dict) or not isinstance(raw.get("datasets"), dict):
        raise ValidationSetupError("Mapping template has an unexpected structure (expected a 'datasets' object).")
    rels = raw.get("relationships", [])
    if not isinstance(rels, list):
        raise ValidationSetupError("Mapping template 'relationships' must be a list.")
    manual_no = 0
    for r in rels:
        if isinstance(r, dict) and not r.get("id"):
            manual_no += 1
            r["id"] = f"M{manual_no:03d}"
    return raw


def resolve_tables(con: duckdb.DuckDBPyConnection, template: dict[str, Any]) -> dict[str, str]:
    """dataset -> raw table. The ingestion manifest wins over the template's table name."""
    manifest: dict[str, str] = {}
    if db.table_exists(con, "meta_ingestion_files"):
        manifest = dict(con.execute("SELECT source_file, target_table FROM meta_ingestion_files").fetchall())
    out = {}
    for name, ds in template["datasets"].items():
        if not isinstance(ds, dict) or name.startswith("_"):
            continue
        table = manifest.get(name) or ds.get("table")
        if isinstance(table, str):
            out[name] = table
    return out


def _table_problem(con: duckdb.DuckDBPyConnection, table: str | None) -> str | None:
    if not table:
        return "no table is mapped for this dataset"
    try:
        db.validate_raw_table_name(table)
    except ValueError:
        return "mapped table name is not a valid raw table name"
    if not db.table_exists(con, table):
        return f"table {table} does not exist (has this dataset been ingested?)"
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' AND table_name = ?", [table]
    ).fetchall()}
    missing = {"_record_key", "payload"} - cols
    if missing:
        return f"table {table} is missing required columns {sorted(missing)}"
    return None


def make_endpoint(con: duckdb.DuckDBPyConnection, end: Any, tables: dict[str, str], side: str) -> Endpoint:
    if not isinstance(end, dict) or not isinstance(end.get("dataset"), str) or not isinstance(end.get("field"), str):
        raise ValidationSetupError(f"'{side}' must contain 'dataset' and 'field'.")
    table = tables.get(end["dataset"])
    problem = _table_problem(con, table)
    if problem:
        raise ValidationSetupError(f"{side} dataset {end['dataset']}: {problem}.")
    try:
        path, is_array = profile_path_to_json(end["field"])
    except ValueError as exc:
        raise ValidationSetupError(f"{side} field {end['field']!r}: {exc}.") from None
    return Endpoint(end["dataset"], end["field"], table, path, is_array)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Aggregate measurements (counts only)
# ---------------------------------------------------------------------------
def side_stats(con: duckdb.DuckDBPyConnection, ep: Endpoint, opts: Options) -> dict[str, Any]:
    v = extraction_sql(ep, opts)
    total_rows = db.row_count(con, ep.table)
    rows_with_key, key_values, distinct_keys = con.execute(
        f"WITH v AS ({v}) SELECT count(DISTINCT rid) FILTER (WHERE k IS NOT NULL), count(k), count(DISTINCT k) FROM v"
    ).fetchone()
    dup_keys, dup_values, max_per_key = con.execute(
        f"WITH v AS ({v}) SELECT count(*), coalesce(sum(n), 0), coalesce(max(n), 0) FROM "
        f"(SELECT k, count(*) AS n FROM v WHERE k IS NOT NULL GROUP BY k HAVING count(*) > 1)"
    ).fetchone()
    return {
        "dataset": ep.dataset,
        "field": ep.field,
        "table": ep.table,
        "is_array_path": ep.is_array,
        "total_rows": int(total_rows),
        "rows_with_key": int(rows_with_key),
        "rows_with_null_key": int(total_rows - rows_with_key),
        "null_key_pct": _pct(total_rows - rows_with_key, total_rows),
        "key_values": int(key_values),
        "distinct_keys": int(distinct_keys),
        "duplicated_keys": int(dup_keys),
        "values_in_duplicated_keys": int(dup_values),
        "extra_duplicate_values": int(key_values - distinct_keys),
        "max_occurrences_per_key": int(max_per_key) if dup_keys else (1 if distinct_keys else 0),
        "is_unique": bool(distinct_keys) and dup_keys == 0,
    }


def match_stats(con: duckdb.DuckDBPyConnection, child: Endpoint, parent: Endpoint, opts: Options) -> dict[str, Any]:
    sql = f"""
    WITH p AS ({extraction_sql(parent, opts)}),
         c AS ({extraction_sql(child, opts)}),
         pk AS MATERIALIZED (SELECT DISTINCT k FROM p WHERE k IS NOT NULL),
         cj AS MATERIALIZED (
             SELECT c.rid, c.k, (pk.k IS NOT NULL) AS hit
             FROM c LEFT JOIN pk ON c.k = pk.k WHERE c.k IS NOT NULL),
         per_parent AS (SELECT k, count(*) AS n FROM cj WHERE hit GROUP BY k)
    SELECT
        (SELECT count(*) FILTER (WHERE hit) FROM cj),
        (SELECT count(*) FILTER (WHERE NOT hit) FROM cj),
        (SELECT count(DISTINCT k) FILTER (WHERE hit) FROM cj),
        (SELECT count(DISTINCT k) FILTER (WHERE NOT hit) FROM cj),
        (SELECT count(DISTINCT rid) FILTER (WHERE NOT hit) FROM cj),
        (SELECT count(*) FROM per_parent),
        (SELECT coalesce(max(n), 0) FROM per_parent),
        (SELECT count(*) FROM pk)
    """
    (matched, unmatched, matched_d, unmatched_d, orphans, referenced_parents,
     max_children, parent_distinct) = (int(x) for x in con.execute(sql).fetchone())
    child_values = matched + unmatched
    child_distinct = matched_d + unmatched_d
    return {
        "matched_values": matched,
        "unmatched_values": unmatched,
        "matched_distinct_keys": matched_d,
        "unmatched_distinct_keys": unmatched_d,
        "overlap_pct_values": _pct(matched, child_values),
        "overlap_pct_distinct": _pct(matched_d, child_distinct),
        "orphan_records": orphans,
        "referenced_parent_keys": referenced_parents,
        "unreferenced_parent_keys": parent_distinct - referenced_parents,
        "parent_coverage_pct": _pct(referenced_parents, parent_distinct),
        "max_children_per_parent": max_children,
        "avg_children_per_referenced_parent": round(matched / referenced_parents, 3) if referenced_parents else None,
    }


def _pct(n: int | float, d: int | float) -> float | None:
    return round(100.0 * n / d, 4) if d else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def observed_cardinality(child: dict[str, Any], parent: dict[str, Any], match: dict[str, Any]) -> str:
    """Cardinality in template vocabulary (source 'from' -> target 'to')."""
    if not parent["is_unique"]:
        return "many-to-many (target key not unique)"
    if child["is_array_path"]:
        return "many-to-many (source holds an array of references)"
    if match["matched_values"] == 0:
        return "undetermined (no matches)"
    if match["max_children_per_parent"] <= 1:
        return "one-to-one"
    return "many-to-one"


def _cardinality_consistent(declared: Any, observed: str) -> bool | None:
    if not isinstance(declared, str) or not declared or observed.startswith("undetermined"):
        return None
    d = declared.split(" ")[0]
    o = observed.split(" ")[0]
    if d == o:
        return True
    # Observed one-to-one is compatible with a declared many-to-one (data just has no repeats).
    return d == "many-to-one" and o == "one-to-one"


def classify(child: dict, parent: dict, match: dict, opts: Options) -> tuple[str, list[str]]:
    findings: list[str] = []
    if parent["key_values"] == 0:
        return ERROR, ["Target field has no values in its table (wrong path, or field never present)."]
    if child["key_values"] == 0:
        return ERROR, ["Source field has no values in its table (wrong path, or field never present)."]

    status = PASS
    if not parent["is_unique"]:
        status = FAIL
        findings.append(f"Referenced key is NOT unique: {parent['duplicated_keys']:,} key values occur more than "
                        f"once ({parent['extra_duplicate_values']:,} extra rows).")
    if parent["rows_with_null_key"]:
        status = max(status, WARN, key=_SEVERITY.get)
        findings.append(f"Referenced key is null/empty in {parent['rows_with_null_key']:,} target rows.")
    if parent["is_array_path"]:
        status = max(status, WARN, key=_SEVERITY.get)
        findings.append("Referenced field is inside an array; a key is normally a scalar field.")

    if match["matched_values"] == 0:
        status = FAIL
        findings.append("No source key matches any target key.")
    elif match["unmatched_values"]:
        overlap = match["overlap_pct_values"] or 0.0
        level = WARN if overlap >= opts.warn_threshold_pct else FAIL
        status = max(status, level, key=_SEVERITY.get)
        findings.append(f"{match['unmatched_values']:,} source key values ({match['unmatched_distinct_keys']:,} "
                        f"distinct) have no matching target; {match['orphan_records']:,} orphan records; "
                        f"overlap {overlap}% (threshold {opts.warn_threshold_pct}%).")
    else:
        findings.append("Every non-null source key matches a target key.")

    if child["rows_with_null_key"]:
        findings.append(f"Source key is null/empty in {child['rows_with_null_key']:,} rows "
                        f"({child['null_key_pct']}%) - optional relationship or missing data.")
    if match["unreferenced_parent_keys"]:
        findings.append(f"{match['unreferenced_parent_keys']:,} target keys are never referenced "
                        f"(parent coverage {match['parent_coverage_pct']}%).")
    return status, findings


# ---------------------------------------------------------------------------
# Validation drivers
# ---------------------------------------------------------------------------
def validate_relationship(con: duckdb.DuckDBPyConnection, rel: dict[str, Any], tables: dict[str, str],
                          opts: Options, *, debug: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": rel.get("id"),
        "confirmed_in_template": rel.get("confirmed") is True,
        "source": rel.get("source"),
        "from": rel.get("from"),
        "to": rel.get("to"),
        "declared_cardinality": rel.get("cardinality"),
        "human_review_required": True,
    }
    try:
        child_ep = make_endpoint(con, rel.get("from"), tables, "from")
        parent_ep = make_endpoint(con, rel.get("to"), tables, "to")
        child = side_stats(con, child_ep, opts)
        parent = side_stats(con, parent_ep, opts)
        match = match_stats(con, child_ep, parent_ep, opts)
    except ValidationSetupError as exc:
        return {**result, "status": ERROR, "findings": [str(exc)]}
    except duckdb.Error as exc:
        if debug:
            traceback.print_exc()  # console only; engine messages may contain values
        return {**result, "status": ERROR, "findings": [f"Query failed ({safe_error(exc)}). Re-run with --debug locally."]}

    status, findings = classify(child, parent, match, opts)
    if status == ERROR:
        observed, consistent = "undetermined (validation error)", None
    else:
        observed = observed_cardinality(child, parent, match)
        consistent = _cardinality_consistent(rel.get("cardinality"), observed)
    if consistent is False:
        status = max(status, WARN, key=_SEVERITY.get)
        findings.append(f"Observed cardinality '{observed}' differs from declared '{rel.get('cardinality')}'.")
    return {
        **result,
        "status": status,
        "observed_cardinality": observed,
        "cardinality_consistent": consistent,
        "source_side": child,
        "target_side": parent,
        "matching": match,
        "findings": findings,
    }


def validate_primary_key(con: duckdb.DuckDBPyConnection, dataset: str, field: str, tables: dict[str, str],
                         opts: Options, *, debug: bool = False) -> dict[str, Any]:
    base = {"dataset": dataset, "field": field, "human_review_required": True}
    try:
        ep = make_endpoint(con, {"dataset": dataset, "field": field}, tables, "primary_key")
        s = side_stats(con, ep, opts)
    except ValidationSetupError as exc:
        return {**base, "status": ERROR, "findings": [str(exc)]}
    except duckdb.Error as exc:
        if debug:
            traceback.print_exc()
        return {**base, "status": ERROR, "findings": [f"Query failed ({safe_error(exc)})."]}
    findings, status = [], PASS
    if s["key_values"] == 0:
        status, findings = ERROR, ["Field has no values (wrong path, or never present)."]
    else:
        if s["duplicated_keys"]:
            status = FAIL
            findings.append(f"{s['duplicated_keys']:,} key values are duplicated ({s['extra_duplicate_values']:,} extra rows).")
        if s["rows_with_null_key"]:
            status = FAIL
            findings.append(f"{s['rows_with_null_key']:,} rows have a null/empty key.")
        if s["is_array_path"]:
            status = max(status, WARN, key=_SEVERITY.get)
            findings.append("Primary key is inside an array.")
        if status == PASS:
            findings.append("Unique and non-null in every row.")
    return {**base, "status": status, "stats": s, "findings": findings}


def select_relationships(template: dict[str, Any], ids: list[str] | None, include_unconfirmed: bool) -> list[dict]:
    rels = [r for r in template.get("relationships", []) if isinstance(r, dict)]
    if ids:
        by_id = {str(r.get("id")): r for r in rels}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValidationSetupError(f"Relationship id(s) not in template: {missing}")
        chosen = [by_id[i] for i in ids]
        unconfirmed = [str(r.get("id")) for r in chosen if r.get("confirmed") is not True]
        if unconfirmed and not include_unconfirmed:
            raise ValidationSetupError(f"Not confirmed in the template: {unconfirmed}. "
                                       "Add --include-unconfirmed to validate them anyway.")
        return chosen
    return rels if include_unconfirmed else [r for r in rels if r.get("confirmed") is True]


def confirmed_primary_keys(template: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    for name, ds in template["datasets"].items():
        pk = ds.get("primary_key") if isinstance(ds, dict) else None
        if isinstance(pk, dict) and pk.get("confirmed") is True and isinstance(pk.get("field"), str):
            out.append((name, pk["field"]))
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def build_report(rel_results: list[dict], pk_results: list[dict], *, db_path: Path, template_path: Path,
                 opts: Options) -> dict[str, Any]:
    counts = {s: sum(1 for r in rel_results if r["status"] == s) for s in _SEVERITY}
    return {
        "validator_version": VALIDATOR_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": "Aggregate counts and percentages only. No key or record values.",
        "human_review_required": True,
        "note": "This tool does not confirm or modify anything. A human must review each result and "
                "update schema_mapping_template.json manually.",
        "database": str(db_path),
        "template": str(template_path),
        "options": {"warn_threshold_pct": opts.warn_threshold_pct, "trim_keys": opts.trim_keys,
                    "case_insensitive": opts.case_insensitive},
        "summary": {"relationships_validated": len(rel_results), "by_status": counts,
                    "primary_keys_validated": len(pk_results),
                    "primary_keys_by_status": {s: sum(1 for r in pk_results if r["status"] == s) for s in _SEVERITY}},
        "primary_keys": pk_results,
        "relationships": rel_results,
    }


def _md_cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v).replace("|", "/").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Relationship validation",
        "",
        f"_Generated {report['generated_at_utc']} - aggregate counts only. **Human review required:** "
        "nothing here is confirmed automatically._",
        "",
        f"Relationships validated: **{s['relationships_validated']}** - "
        + ", ".join(f"{k}: {v}" for k, v in s["by_status"].items()),
        "",
    ]
    if report["primary_keys"]:
        lines += ["## Primary keys", "", "| dataset | field | status | rows | null keys | distinct | duplicated keys |",
                  "|---|---|---|---|---|---|---|"]
        for p in report["primary_keys"]:
            st = p.get("stats", {})
            lines.append("| " + " | ".join(_md_cell(x) for x in (
                p["dataset"], p["field"], p["status"], st.get("total_rows"), st.get("rows_with_null_key"),
                st.get("distinct_keys"), st.get("duplicated_keys"))) + " |")
        lines.append("")

    lines += ["## Relationships overview", "",
              "| id | from | to | status | overlap % (values) | orphan records | observed cardinality |",
              "|---|---|---|---|---|---|---|"]
    for r in report["relationships"]:
        f, t, m = r.get("from") or {}, r.get("to") or {}, r.get("matching") or {}
        lines.append("| " + " | ".join(_md_cell(x) for x in (
            r["id"], f"{f.get('dataset')}.{f.get('field')}", f"{t.get('dataset')}.{t.get('field')}", r["status"],
            m.get("overlap_pct_values"), m.get("orphan_records"), r.get("observed_cardinality"))) + " |")
    lines.append("")

    for r in report["relationships"]:
        f, t = r.get("from") or {}, r.get("to") or {}
        lines += [f"## {r['id']} - {_md_cell(r['status'])}", "",
                  f"`{f.get('dataset')}.{f.get('field')}` -> `{t.get('dataset')}.{t.get('field')}`  ",
                  f"Confirmed in template: {_md_cell(r['confirmed_in_template'])} - declared cardinality: "
                  f"{_md_cell(r.get('declared_cardinality'))} - observed: {_md_cell(r.get('observed_cardinality'))}", ""]
        if "source_side" in r:
            cs, ps, m = r["source_side"], r["target_side"], r["matching"]
            lines += ["| measure | source (from) | target (to) |", "|---|---|---|"]
            for label, key in (("total rows", "total_rows"), ("rows with null key", "rows_with_null_key"),
                               ("key values", "key_values"), ("distinct keys", "distinct_keys"),
                               ("duplicated keys", "duplicated_keys"), ("extra duplicate values", "extra_duplicate_values"),
                               ("unique", "is_unique")):
                lines.append(f"| {label} | {_md_cell(cs[key])} | {_md_cell(ps[key])} |")
            lines += ["", "| matching | value |", "|---|---|"]
            for label, key in (("matched values", "matched_values"), ("unmatched values", "unmatched_values"),
                               ("matched distinct keys", "matched_distinct_keys"),
                               ("unmatched distinct keys", "unmatched_distinct_keys"),
                               ("overlap % (values)", "overlap_pct_values"), ("overlap % (distinct)", "overlap_pct_distinct"),
                               ("orphan records", "orphan_records"), ("parent coverage %", "parent_coverage_pct"),
                               ("max children per parent", "max_children_per_parent")):
                lines.append(f"| {label} | {_md_cell(m[key])} |")
            lines.append("")
        lines += [f"- {_md_cell(x)}" for x in r["findings"]] + [""]
    return "\n".join(lines) + "\n"


def print_summary(report: dict[str, Any]) -> None:
    print(f"\n{'id':<6} {'status':<22} {'overlap%':>9} {'orphans':>10} {'unmatched':>10}  observed cardinality")
    for r in report["relationships"]:
        m = r.get("matching") or {}
        ov = m.get("overlap_pct_values")
        print(f"{str(r['id']):<6} {r['status']:<22} {(f'{ov:.2f}' if ov is not None else '-'):>9} "
              f"{m.get('orphan_records', '-'):>10} {m.get('unmatched_values', '-'):>10}  "
              f"{r.get('observed_cardinality', '-')}")
        if r["status"] == ERROR:
            print(f"       {r['findings'][0]}")
    for p in report["primary_keys"]:
        print(f"PK  {p['dataset']}.{p['field']}: {p['status']}")
    print("\nHuman review required - nothing was confirmed or changed in the template.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate confirmed relationships in local DuckDB (counts only).")
    p.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--output-dir", type=Path, default=config.REPORTS_DIR)
    p.add_argument("--relationship", "-r", action="append", metavar="ID", help="Validate only this id (repeatable).")
    p.add_argument("--include-unconfirmed", action="store_true", help="Also validate unconfirmed candidates.")
    p.add_argument("--skip-primary-keys", action="store_true", help="Do not validate confirmed primary keys.")
    p.add_argument("--warn-threshold", type=float, default=90.0,
                   help="Overlap %% at/above which orphans give 'warning' instead of 'failed' (default 90).")
    p.add_argument("--trim-keys", action="store_true", help="Ignore leading/trailing whitespace in keys.")
    p.add_argument("--case-insensitive", action="store_true", help="Compare keys case-insensitively.")
    p.add_argument("--list", action="store_true", help="List relationships in the template and exit.")
    p.add_argument("--force", action="store_true", help="Overwrite existing reports without asking (.bak kept).")
    p.add_argument("--debug", action="store_true", help="Print query tracebacks to the console (may include values).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not 0 <= args.warn_threshold <= 100:
        print("ERROR: --warn-threshold must be between 0 and 100", file=sys.stderr)
        return 2
    opts = Options(args.warn_threshold, args.trim_keys, args.case_insensitive)
    template_path, db_path, out_dir = args.template.resolve(), args.db.resolve(), args.output_dir.resolve()

    try:
        template = load_template(template_path)
        if args.list:
            for r in template.get("relationships", []):
                if isinstance(r, dict):
                    f, t = r.get("from") or {}, r.get("to") or {}
                    print(f"{str(r.get('id')):<6} confirmed={str(r.get('confirmed') is True):<5} "
                          f"{f.get('dataset')}.{f.get('field')} -> {t.get('dataset')}.{t.get('field')}")
            return 0
        rels = select_relationships(template, args.relationship, args.include_unconfirmed)
        pks = [] if args.skip_primary_keys or args.relationship else confirmed_primary_keys(template)
        if not rels and not pks:
            print("Nothing to validate: no relationships or primary keys are marked \"confirmed\": true "
                  "(use --include-unconfirmed to test candidates).", file=sys.stderr)
            return 2
        if not db_path.exists():
            raise ValidationSetupError(f"Database not found: {db_path}. Run init_database.py and ingestion.py first.")

        with db.db_connection(db_path, read_only=True) as con:
            tables = resolve_tables(con, template)
            rel_results = []
            for i, r in enumerate(rels, start=1):
                print(f"[{i}/{len(rels)}] validating {r.get('id')} ...", file=sys.stderr, flush=True)
                rel_results.append(validate_relationship(con, r, tables, opts, debug=args.debug))
            pk_results = [validate_primary_key(con, d, f, tables, opts, debug=args.debug) for d, f in pks]
    except ValidationSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except duckdb.IOException as exc:
        print(f"ERROR: cannot open database read-only ({safe_error(exc)}). Is ingestion still running?", file=sys.stderr)
        return 3
    except duckdb.Error as exc:
        print(f"ERROR: database error ({safe_error(exc)}).", file=sys.stderr)
        return 1

    report = build_report(rel_results, pk_results, db_path=db_path, template_path=template_path, opts=opts)
    print_summary(report)

    paths = [out_dir / JSON_REPORT, out_dir / MD_REPORT]
    if confirm_overwrite(paths, force=args.force):
        write_json_atomic(paths[0], report)
        write_text_atomic(paths[1], render_markdown(report))
        print("Wrote:\n  " + "\n  ".join(str(p) for p in paths))
    else:
        print("Reports not written.")

    worst = max((_SEVERITY[r["status"]] for r in rel_results + pk_results), default=0)
    return 1 if worst >= _SEVERITY[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
