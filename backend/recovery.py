"""
Local recovery & demo orchestration for Blast Intelligence AI.

    python backend/recovery.py check          # state of every pipeline stage + services (counts only)
    python backend/recovery.py validate       # strict automatic relationship / primary-key decisions
    python backend/recovery.py prepare-demo   # typed views + conservative analytics mapping + analytics run
    python backend/recovery.py test           # synthetic backend tests + frontend tests + production build

Nothing here invents a calculation: it drives the existing modules
(schema_analyzer, relationship_validator, typed_views, analytics) and replaces
manual approval with a STRICT, documented policy. Anything that does not pass
is left disabled and reported as unavailable - never approved "to make it work".

Automatic decision policy
-------------------------
Relationship (approved only if ALL hold; measured in DuckDB, counts only):
  * relationship_validator status == passed_pending_review
  * target key unique and never null; source is a scalar (not an array)
  * 100 % of non-null source values match (0 unmatched, 0 orphan records)
  * >= 100 matched values and >= 20 distinct matched keys (no tiny coincidences)
  * source key null in <= 50 % of rows
  * observed cardinality many-to-one or one-to-one, consistent with the candidate
  * no other candidate for the same source field also passes (ambiguous => none)
Primary key: the analyzer's best candidate that is unique and non-null in DuckDB.
Columns: included only if the profile gives them a scalar role, they have values
  in >= 5 % of rows and convert with <= 1 % failures (otherwise kept as text or excluded).
Analytics roles: each mapped column must pass value-based plausibility checks
  (type, coverage, ranges, cardinality); e.g. durations only from a valid
  start/end pair or an explicit unit in the name; completion only from a boolean
  status; "problems" only from a dataset whose name says so.

Human decisions are never overwritten: entries without ``decided_by: recovery-auto``
(and every ``source: manual`` relationship) are kept exactly as they are.

Privacy: reads only the local DuckDB file and local reports; raw tables are never
modified; console output is limited to counts, ids and reason codes. Detailed
decisions go to local_reports/recovery_report.{json,md} (Git-ignored).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import shutil
import socket
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import analytics_mapping as am  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402
import relationship_validator as rv  # noqa: E402
import typed_views as tv  # noqa: E402
from report_io import ReportError, load_json_report, write_json_atomic, write_text_atomic  # noqa: E402

AUTO = "recovery-auto"
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

INCLUDABLE_ROLES = {"numeric", "numeric_string", "binary_flag_numeric", "boolean", "date_like_string",
                    "date_like_epoch", "categorical", "identifier_like", "string"}
PROBLEM_TOKENS = {"problem", "problems", "issue", "issues", "incident", "incidents", "fault", "faults",
                  "failure", "failures", "defect", "defects"}
COMPLETION_TOKENS = {"complete", "completed", "done", "finished"}
UNIT_TOKENS = {"minutes": {"min", "mins", "minute", "minutes"}, "seconds": {"sec", "secs", "second", "seconds"},
               "hours": {"hr", "hrs", "hour", "hours"}}
ID_WORDS = {"id", "ids", "key", "uuid", "guid", "code", "ref", "no", "number", "num"}


class RecoveryError(Exception):
    """A precondition failed. The message names the fix and contains no data values."""


@dataclass(frozen=True)
class Policy:
    min_matched_values: int = 100
    min_matched_distinct: int = 20
    max_source_null_pct: float = 50.0
    max_conversion_failure_pct: float = 1.0
    min_column_present_share: float = 0.05
    min_entity_rows: int = 20
    min_time_share: float = 0.5
    max_label_levels: int = 500
    max_category_levels: int = 200
    max_pk_candidates: int = 5
    max_measures: int = 10


@dataclass(frozen=True)
class Paths:
    db: Path
    data_dir: Path
    reports: Path
    salt: Path
    models: Path

    @property
    def profile(self) -> Path:
        return self.reports / "data_profile.json"

    @property
    def template(self) -> Path:
        return self.reports / "schema_mapping_template.json"

    @property
    def validation(self) -> Path:
        return self.reports / rv.JSON_REPORT

    @property
    def mapping(self) -> Path:
        return self.reports / "analytics_mapping.json"

    @property
    def report(self) -> Path:
        return self.reports / "recovery_report.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quiet():
    """Silence the verbose console output of the modules we drive (their files are still written)."""
    return contextlib.redirect_stdout(io.StringIO())


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _local_get(url: str, timeout: float = 5.0) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _is_human(entry: dict[str, Any]) -> bool:
    """A decision someone made by hand: confirmed without our marker, or a manual relationship."""
    return entry.get("source") == "manual" or (entry.get("confirmed") is True and entry.get("decided_by") != AUTO)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def ensure_template(paths: Paths) -> None:
    if paths.template.exists():
        return
    if not paths.profile.exists():
        raise RecoveryError("data_profile.json is missing - run: python backend\\data_inspector.py")
    import schema_analyzer

    with _quiet():
        rc = schema_analyzer.main(["--profile", str(paths.profile), "--output-dir", str(paths.reports),
                                   "--force", "--no-interactive"])
    if rc != 0 or not paths.template.exists():
        raise RecoveryError("schema analysis failed - run python backend\\schema_analyzer.py to see why")


def judge_relationship(res: dict[str, Any], policy: Policy) -> list[str]:
    """Return rejection reason codes ([] means it meets every strict criterion)."""
    if res.get("status") == rv.ERROR or "matching" not in res:
        return ["validation_error"]
    reasons = []
    if res["status"] != rv.PASS:
        reasons.append(f"validation_{res['status']}")
    c, t, m = res["source_side"], res["target_side"], res["matching"]
    if not t["is_unique"]:
        reasons.append("target_not_unique")
    if t["rows_with_null_key"]:
        reasons.append("target_has_null_keys")
    if c["is_array_path"]:
        reasons.append("array_reference")
    if m["unmatched_values"] or m["orphan_records"]:
        reasons.append("orphan_records")
    if m["matched_values"] < policy.min_matched_values:
        reasons.append("too_few_matched_values")
    if m["matched_distinct_keys"] < policy.min_matched_distinct:
        reasons.append("too_few_distinct_keys")
    if (c["null_key_pct"] or 0) > policy.max_source_null_pct:
        reasons.append("source_key_mostly_null")
    if res.get("observed_cardinality") not in ("many-to-one", "one-to-one"):
        reasons.append("cardinality_not_supported")
    if res.get("cardinality_consistent") is False:
        reasons.append("cardinality_mismatch")
    return reasons


def _evidence(res: dict[str, Any]) -> dict[str, Any]:
    if "matching" not in res:
        return {"status": res.get("status")}
    c, t, m = res["source_side"], res["target_side"], res["matching"]
    return {"status": res["status"], "matched_values": m["matched_values"], "unmatched_values": m["unmatched_values"],
            "orphan_records": m["orphan_records"], "overlap_pct_values": m["overlap_pct_values"],
            "matched_distinct_keys": m["matched_distinct_keys"], "source_null_pct": c["null_key_pct"],
            "target_unique": t["is_unique"], "target_null_keys": t["rows_with_null_key"],
            "observed_cardinality": res.get("observed_cardinality")}


def validate_stage(paths: Paths, policy: Policy) -> dict[str, Any]:
    if not paths.db.exists():
        raise RecoveryError(f"database not found at {paths.db} - run init_database.py and ingestion.py")
    ensure_template(paths)
    try:
        template = rv.load_template(paths.template)
    except rv.ValidationSetupError as exc:
        raise RecoveryError(str(exc)) from None
    opts = rv.Options()
    pk_summary, rel_decisions, results = {}, {}, {}

    try:
        con_ctx = db.db_connection(paths.db, read_only=True)
        con = con_ctx.__enter__()
    except duckdb.IOException:
        raise RecoveryError("database is locked by another process (stop ingestion/notebooks)") from None
    try:
        tables = rv.resolve_tables(con, template)
        # -- primary keys ----------------------------------------------------
        pk_results = []
        for name, ds in template["datasets"].items():
            if not isinstance(ds, dict) or name.startswith("_"):
                continue
            pk = ds.setdefault("primary_key", {})
            if _is_human(pk):
                pk_summary[name] = "kept_human"
                if isinstance(pk.get("field"), str):
                    pk_results.append(rv.validate_primary_key(con, name, pk["field"], tables, opts))
                continue
            chosen, tried = None, []
            for cand in (pk.get("candidates") or [])[: policy.max_pk_candidates]:
                field = cand.get("field") if isinstance(cand, dict) else None
                if not isinstance(field, str):
                    continue
                r = rv.validate_primary_key(con, name, field, tables, opts)
                tried.append({"field": field, "status": r["status"]})
                if r["status"] == rv.PASS:
                    chosen = (field, r)
                    break
            if chosen:
                pk.update(field=chosen[0], confirmed=True, decided_by=AUTO,
                          auto_evidence={k: chosen[1]["stats"][k] for k in ("total_rows", "distinct_keys",
                                                                             "rows_with_null_key", "duplicated_keys")})
                pk_results.append(chosen[1])
                pk_summary[name] = "approved"
            else:
                pk.update(confirmed=False, decided_by=AUTO,
                          auto_reason="no candidate is unique and non-null" if tried else "no candidate")
                pk_summary[name] = "none"
        # -- relationships ----------------------------------------------------
        rels = [r for r in template.get("relationships", []) if isinstance(r, dict)]
        for rel in rels:
            results[rel["id"]] = rv.validate_relationship(con, rel, tables, opts)
    finally:
        con_ctx.__exit__(None, None, None)

    passing = defaultdict(list)
    for rel in rels:
        if _is_human(rel):
            continue
        reasons = judge_relationship(results[rel["id"]], policy)
        rel_decisions[rel["id"]] = reasons
        if not reasons:
            f = rel.get("from") or {}
            passing[(f.get("dataset"), f.get("field"))].append(rel["id"])
    for ids in passing.values():
        if len(ids) > 1:                                  # same source field fits several targets
            for rid in ids:
                rel_decisions[rid] = ["ambiguous_source_field"]

    decisions = []
    for rel in rels:
        res = results[rel["id"]]
        if _is_human(rel):
            decision, reasons = "kept_human", []
        else:
            reasons = rel_decisions[rel["id"]]
            decision = "rejected" if reasons else "approved"
            rel.update(confirmed=not reasons, decided_by=AUTO, auto_decision=decision, auto_reasons=reasons,
                       auto_evidence=_evidence(res))
        res["confirmed_in_template"] = rel.get("confirmed") is True
        decisions.append({"id": rel["id"], "from": rel.get("from"), "to": rel.get("to"), "decision": decision,
                          "reasons": reasons, "evidence": _evidence(res)})

    write_json_atomic(paths.template, template)
    confirmed_results = [results[r["id"]] for r in rels if r.get("confirmed") is True]
    report = rv.build_report(confirmed_results, pk_results, db_path=paths.db, template_path=paths.template, opts=opts)
    write_json_atomic(paths.validation, report)
    write_text_atomic(paths.validation.with_suffix(".md"), rv.render_markdown(report))

    counts = Counter(d["decision"] for d in decisions)
    reason_counts = Counter(r for d in decisions for r in d["reasons"])
    summary = {"relationships": {"candidates": len(decisions), **dict(counts),
                                 "rejection_reasons": dict(reason_counts.most_common())},
               "primary_keys": dict(Counter(pk_summary.values())), "decisions": decisions,
               "policy": asdict(policy)}
    print(f"Relationships: {len(decisions)} candidates -> approved {counts.get('approved', 0)}, "
          f"rejected {counts.get('rejected', 0)}, kept (human) {counts.get('kept_human', 0)}")
    for reason, n in reason_counts.most_common():
        print(f"  rejected because {reason}: {n}")
    for d in decisions:
        if d["decision"] == "approved":
            print(f"  APPROVED {d['id']} ({d['evidence'].get('matched_values'):,} matched values, "
                  f"{d['evidence'].get('observed_cardinality')})")
    print(f"Primary keys: {dict(Counter(pk_summary.values()))} (of {len(pk_summary)} datasets)")
    return summary


# ---------------------------------------------------------------------------
# prepare-demo: columns -> typed views -> analytics mapping -> analytics run
# ---------------------------------------------------------------------------
def select_columns(con: duckdb.DuckDBPyConnection, template: dict[str, Any], policy: Policy) -> dict[str, Any]:
    tables = rv.resolve_tables(con, template)
    summary: dict[str, Counter] = {}
    for name, ds in template["datasets"].items():
        if not isinstance(ds, dict) or name.startswith("_"):
            continue
        cols = [c for c in ds.get("columns", []) if isinstance(c, dict)]
        if any(c.get("include") is True and c.get("decided_by") != AUTO for c in cols):
            summary[name] = Counter(kept_human=1)        # someone chose columns by hand - leave them
            continue
        for c in cols:
            # Re-evaluated from scratch on every run (earlier automatic downgrades are re-measured).
            auto_ok = c.get("role") in INCLUDABLE_ROLES and bool(c.get("suggested_type"))
            c.update(include=bool(auto_ok), type=c.get("suggested_type") or c.get("type") or "VARCHAR",
                     decided_by=AUTO, auto_reason=None if auto_ok else f"role {c.get('role')} not auto-included")
    plans, _, _ = tv.build_plan(template, None, tables, allow_warnings=False)
    by_ds = {p.dataset: p for p in plans}
    for name, ds in template["datasets"].items():
        if not isinstance(ds, dict) or name.startswith("_") or name in summary:
            continue
        plan = by_ds.get(name)
        counter: Counter = Counter()
        if plan is None:
            summary[name] = counter
            continue
        tv.measure_dataset(con, plan, warn_pct=policy.max_conversion_failure_pct)
        planned = {c.field: c for c in plan.columns}
        for c in ds.get("columns", []):
            if not isinstance(c, dict) or c.get("decided_by") != AUTO or not c.get("include"):
                continue
            p = planned.get(c["field"])
            st = p.stats if p else {}
            check_failed = [m for m in (p.messages if p else []) if "could not complete" in m]
            if p is None or p.status == "error" and not st:
                c.update(include=False, auto_reason="cannot be a view column (name/path)")
                counter["excluded_invalid"] += 1
            elif check_failed:                            # never approve a column whose check did not finish
                c.update(include=False, auto_reason=check_failed[0])
                counter["excluded_check_failed"] += 1
            elif not st.get("rows") or st.get("present", 0) < policy.min_column_present_share * st["rows"]:
                c.update(include=False, auto_reason="too sparse (< 5% of rows have a value)")
                counter["excluded_sparse"] += 1
            elif (st.get("conversion_failure_pct") or 0) > policy.max_conversion_failure_pct and c["type"] != "VARCHAR":
                c.update(type="VARCHAR", auto_reason=f"{c['type']} conversion failed for "
                                                     f"{st['conversion_failure_pct']}% - kept as text")
                counter["downgraded_to_text"] += 1
            elif p.status == "error":
                c.update(include=False, auto_reason="; ".join(p.messages) or "column error")
                counter["excluded_error"] += 1
            else:
                counter["included"] += 1
        summary[name] = counter
    return {k: dict(v) for k, v in summary.items()}


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> tuple:
    return con.execute(sql).fetchone()


def check_role(con, view: str, role: str, col: str, dtype: str, rows: int, entity: str,
               policy: Policy) -> tuple[bool, str]:
    """Value-based plausibility check for one role -> (ok, reason). Only aggregates are read."""
    v, q = db.quote_ident(view), db.quote_ident(col)
    kind = am.ENTITY_ROLES[entity][role]
    k = max(5, policy.min_entity_rows // 4)
    if kind in ("time", "timestamp"):
        n, ok = _scalar(con, f"SELECT count({q}), count(*) FILTER (WHERE {q} >= TIMESTAMP '1990-01-01' "
                             f"AND {q} < TIMESTAMP '2100-01-01') FROM {v}")
        if n < max(k, policy.min_time_share * rows):
            return False, "time column too sparse"
        return (ok >= 0.99 * n), "timestamps outside 1990-2100"
    if role == "depth":
        n, pos, med = _scalar(con, f"SELECT count({q}), count(*) FILTER (WHERE {q} > 0), "
                                   f"median({q}) FILTER (WHERE {q} > 0) FROM {v}")
        if n < max(k, 0.2 * rows):
            return False, "depth too sparse"
        if pos < 0.95 * n or med is None or not (0.1 <= med <= 1000):
            return False, "values not plausible as hole depth"
        return True, ""
    if role == "duration":
        unit = next((u for u, toks in UNIT_TOKENS.items() if set(am.name_tokens(col)) & toks), None)
        if unit is None:
            return False, "duration unit not stated in the column name"
        n, nonneg, med = _scalar(con, f"SELECT count({q}), count(*) FILTER (WHERE {q} >= 0), "
                                      f"median({q}) FILTER (WHERE {q} > 0) FROM {v}")
        return (n >= k and nonneg >= 0.95 * n and bool(med)), "values not plausible as durations"
    if role == "status":
        if dtype.upper() == "BOOLEAN" and set(am.name_tokens(col)) & COMPLETION_TOKENS:
            (n,) = _scalar(con, f"SELECT count({q}) FROM {v}")
            return n >= k, "status too sparse"
        return False, "completed status values unknown (needs a boolean 'completed'-style column)"
    if kind == "bool":
        (n,) = _scalar(con, f"SELECT count({q}) FROM {v}")
        return n >= k, "flag too sparse"
    n, distinct = _scalar(con, f"SELECT count({q}), count(DISTINCT {q}) FROM {v}")
    if kind == "label":
        levels = policy.max_category_levels if role == "category" else policy.max_label_levels
        if n < max(k, 0.2 * rows):
            return False, "label too sparse"
        if distinct < 2 or distinct > levels or distinct > 0.5 * n:
            return False, "cardinality not plausible for a grouping label"
        return True, ""
    own_id = (entity, role) in (("holes", "hole_id"), ("blasts", "blast_id"), ("problems", "problem_id"))
    if n < max(k, (0.5 if own_id else 0.2) * rows) or distinct < 2:
        return False, "identifier too sparse"
    if entity == "blasts" and role == "blast_id" and distinct < 0.9 * n:
        return False, "blast id not unique per blast row"
    return True, ""


def evaluate_entity(con, entity: str, view: str, cols: dict[str, str], policy: Policy) -> dict[str, Any]:
    (rows,) = _scalar(con, f"SELECT count(*) FROM {db.quote_ident(view)}")
    out: dict[str, Any] = {"view": view, "rows": int(rows), "columns": {}, "dropped": {}, "measures": [],
                           "duration_unit": "minutes", "confirmable": False, "reason": None}
    if rows < policy.min_entity_rows:
        out["reason"] = f"fewer than {policy.min_entity_rows} rows"
        return out
    used: set[str] = set()
    for role in am.ENTITY_ROLES[entity]:
        ranked = sorted(((c, am._role_score(role, c, t)) for c, t in cols.items() if not c.startswith("_")),
                        key=lambda x: -x[1])
        why = "no column name/type matches this role"
        for c, score in ranked[:4]:
            if score <= 0 or c in used:
                continue
            ok, why = check_role(con, view, role, c, cols[c], rows, entity, policy)
            if ok:
                out["columns"][role] = c
                used.add(c)
                if role == "duration":
                    out["duration_unit"] = next(u for u, t in UNIT_TOKENS.items() if set(am.name_tokens(c)) & t)
                break
        if role not in out["columns"]:
            out["dropped"][role] = why
    m = out["columns"]
    if "start_time" in m and "end_time" in m:
        s, e = db.quote_ident(m["start_time"]), db.quote_ident(m["end_time"])
        both, pos, med = _scalar(con, f"SELECT count(*) FILTER (WHERE {s} IS NOT NULL AND {e} IS NOT NULL), "
                                      f"count(*) FILTER (WHERE {e} > {s}), median(date_diff('second', {s}, {e})) "
                                      f"FILTER (WHERE {e} > {s}) FROM {db.quote_ident(view)}")
        if both < policy.min_entity_rows or pos < 0.95 * both or med is None or not (60 <= med <= 7 * 86400):
            out["dropped"]["end_time"] = "start/end pair not plausible as drilling intervals"
            used.discard(m.pop("end_time"))
        elif "duration" in m:
            out["dropped"]["duration"] = "durations computed from the validated start/end pair instead"
            used.discard(m.pop("duration"))
    if entity in ("holes", "blasts"):
        for c, t in cols.items():
            if len(out["measures"]) >= policy.max_measures or c.startswith("_") or c in used:
                continue
            if not am.kind_ok("num", t) or set(am.name_tokens(c)) & ID_WORDS:
                continue
            n, distinct = _scalar(con, f"SELECT count({db.quote_ident(c)}), count(DISTINCT {db.quote_ident(c)}) "
                                       f"FROM {db.quote_ident(view)}")
            if n >= 0.5 * rows and distinct >= 3:
                out["measures"].append(c)
    needs = {"holes": ("hole_id", "depth", "start_time"), "blasts": ("blast_id",), "problems": ("time", "category")}
    out["confirmable"] = any(r in m for r in needs[entity])
    if not out["confirmable"]:
        out["reason"] = f"none of the required roles {needs[entity]} passed the checks"
    return out


def auto_mapping(con, views: dict[str, dict[str, str]], policy: Policy) -> tuple[dict[str, Any], dict[str, Any]]:
    entities: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    used_views: set[str] = set()
    for entity in ("holes", "blasts", "problems"):
        cands = sorted((v for v in views if v.startswith(tv.DATASET_VIEW_PREFIX) and v not in used_views
                        and am._view_score(entity, v) > 0), key=lambda v: (-am._view_score(entity, v), v))
        if entity == "problems":
            cands = [v for v in cands if set(am.name_tokens(v)) & PROBLEM_TOKENS]
        chosen, attempts = None, []
        for view in cands[:3]:
            res = evaluate_entity(con, entity, view, views[view], policy)
            if res["confirmable"]:
                chosen = res
                break
            attempts.append({"view": view, "reason": res["reason"]})
        roles = am.ENTITY_ROLES[entity]
        if chosen:
            used_views.add(chosen["view"])
            entry = {"description": am.ENTITY_DESCRIPTIONS[entity], "confirmed": True, "view": chosen["view"],
                     "columns": {r: chosen["columns"].get(r) for r in roles}, "measures": chosen["measures"],
                     "decided_by": AUTO, "auto_checks": {"rows": chosen["rows"], "roles_dropped": chosen["dropped"]}}
            if entity == "holes":
                entry.update(duration_unit=chosen["duration_unit"], completed_values=[])
            summary[entity] = {"confirmed": True, "roles": sorted(chosen["columns"]),
                               "roles_unavailable": sorted(chosen["dropped"]), "measures": len(chosen["measures"])}
        else:
            reason = ("no dataset name identifies drilling problems" if entity == "problems" and not cands
                      else "no candidate view passed the checks" if cands else "no candidate view")
            entry = {"description": am.ENTITY_DESCRIPTIONS[entity], "confirmed": False, "view": None,
                     "columns": dict.fromkeys(roles), "measures": [], "decided_by": AUTO,
                     "auto_checks": {"reason": reason, "attempts": attempts}}
            summary[entity] = {"confirmed": False, "reason": reason}
        entities[entity] = entry
    mapping = {
        "_instructions": ["Generated by recovery.py with conservative automatic checks (decided_by: recovery-auto).",
                          "Edit any entity and remove its decided_by key to make it a human decision; "
                          "recovery.py will then never change it."],
        "mapping_version": am.MAPPING_VERSION,
        "privacy": {"min_group_size": 5, "pseudonymize_labels": True},
        "entities": entities,
    }
    return mapping, summary


def _backend_running() -> bool:
    return _port_open(config.API_PORT)


def _db_failure(step: str, exc: BaseException, state: str) -> str:
    hint = ""
    if isinstance(exc, duckdb.OutOfMemoryException):
        hint = (" DuckDB ran out of memory: try $env:BLAST_MEASURE_BATCH_COLUMNS='4' and/or "
                "$env:BLAST_DUCKDB_THREADS='2', or raise $env:BLAST_DUCKDB_MEMORY_LIMIT (current "
                f"{config.DUCKDB_MEMORY_LIMIT}), then re-run.")
    return f"{step} failed ({type(exc).__name__}); {state}.{hint}"


def prepare_stage(paths: Paths, policy: Policy) -> dict[str, Any]:
    if _backend_running():
        raise RecoveryError(f"the backend is running on port {config.API_PORT} and holds the database open - "
                            "stop it (Ctrl+C or scripts\\stop-local.ps1) and re-run prepare-demo")
    if not paths.db.exists():
        raise RecoveryError(f"database not found at {paths.db}")
    try:
        with db.db_connection(paths.db) as _probe:       # confirms write access (no writes happen here)
            pass
    except duckdb.IOException:
        raise RecoveryError("database is locked by another process (backend, notebook, ingestion?)") from None

    template = load_json_report(paths.template, what="Mapping template") if paths.template.exists() else None
    if template is None or not paths.validation.exists() or not any(
            isinstance(r, dict) and r.get("decided_by") == AUTO or _is_human(r) for r in template.get("relationships", [])):
        print("Running validate first ...")
        validate_stage(paths, policy)
    template = rv.load_template(paths.template)

    print(f"Selecting columns (conversion checks, {tv.MEASURE_BATCH_COLUMNS} columns per query) ...")
    candidate = copy.deepcopy(template)          # all-or-nothing: the template is written only on full success
    try:
        with db.db_connection(paths.db, read_only=True) as con:
            col_summary = select_columns(con, candidate, policy)
    except duckdb.Error as exc:
        raise RecoveryError(_db_failure("column selection", exc, "schema_mapping_template.json was not changed")) from None
    write_json_atomic(paths.template, candidate)
    template = candidate
    for name, c in col_summary.items():
        print(f"  {name}: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    failed = sum(c.get("excluded_check_failed", 0) for c in col_summary.values())
    if failed:
        print(f"  WARNING: {failed} column(s) excluded because their conversion check could not complete "
              "(not approved; see recovery_report.md). Lower BLAST_MEASURE_BATCH_COLUMNS or raise "
              "BLAST_DUCKDB_MEMORY_LIMIT and re-run to include them.")

    print("Creating typed views ...")
    with _quiet():
        tv.main(["--template", str(paths.template), "--validation", str(paths.validation), "--db", str(paths.db),
                 "--output-dir", str(paths.reports), "--force"])
    tv_report = load_json_report(paths.reports / tv.JSON_REPORT, what="Typed views summary")
    if not tv_report.get("views_created"):
        raise RecoveryError("typed views were not created - see local_reports\\typed_views_summary.md "
                            "(errors: " + "; ".join(tv_report.get("errors", [])[:3]) + ")")
    joins = [j for j in tv_report["join_views"] if j["status"] == "created"]
    print(f"  dataset views: {len(tv_report['dataset_views'])}, join views: {len(joins)}")

    print("Generating the analytics mapping (plausibility checks) ...")
    try:
        with db.db_connection(paths.db, read_only=True) as con:
            views = am.approved_views(con)
            mapping, map_summary = auto_mapping(con, views, policy)
    except duckdb.Error as exc:
        raise RecoveryError(_db_failure("analytics mapping checks", exc,
                                        "analytics_mapping.json was not written (typed views are in place)")) from None
    target = paths.mapping
    if paths.mapping.exists():
        try:
            existing = load_json_report(paths.mapping, what="Analytics mapping")
        except ReportError:
            existing = {}
        human = [n for n, e in (existing.get("entities") or {}).items()
                 if isinstance(e, dict) and e.get("confirmed") is True and e.get("decided_by") != AUTO]
        if human:
            target = paths.mapping.with_name("analytics_mapping.auto.json")
            print(f"  existing mapping has human-confirmed entities {human} - kept; suggestion written to {target.name}")
    write_json_atomic(target, mapping)
    for ent, s in map_summary.items():
        print(f"  {ent:<9} " + (f"confirmed - roles {len(s['roles'])}, unavailable {len(s['roles_unavailable'])}, "
                                f"measures {s['measures']}" if s["confirmed"] else f"not confirmed ({s['reason']})"))

    print("Running analytics ...")
    import analytics

    with _quiet():
        rc = analytics.main(["run", "--db", str(paths.db), "--mapping", str(paths.mapping), "--output-dir",
                             str(paths.reports), "--force", "--salt-file", str(paths.salt),
                             "--models-dir", str(paths.models)])
    availability: list[dict] = []
    if rc == 0 and (paths.reports / analytics.JSON_REPORT).exists():
        availability = load_json_report(paths.reports / analytics.JSON_REPORT, what="Analytics summary")["availability"]
        ok = [a for a in availability if a["status"] == "ok"]
        print(f"  metrics available: {len(ok)} of {len(availability)}")
        for a in availability:
            if a["status"] != "ok":
                print(f"    unavailable: {a['title']}")
    else:
        print("  analytics run did not complete - run: python backend\\analytics.py run")

    summary = {"columns": col_summary,
               "typed_views": {"dataset_views": len(tv_report["dataset_views"]), "join_views": len(joins),
                               "relationships_not_joined": len(tv_report.get("relationships_not_joined", []))},
               "analytics_mapping": {"file": target.name, "entities": map_summary},
               "analytics": {"exit_code": rc, "availability": availability}}
    return summary


# ---------------------------------------------------------------------------
# check / test
# ---------------------------------------------------------------------------
def check_stage(paths: Paths) -> int:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, item: str, detail: str = "") -> None:
        rows.append((status, item, detail))

    jsonl = list(paths.data_dir.glob("*.jsonl")) if paths.data_dir.is_dir() else []
    add("OK" if jsonl else "!!", "source JSONL files", f"{len(jsonl)} files, {sum(p.stat().st_size for p in jsonl) / 1e6:,.0f} MB")
    add("OK" if paths.profile.exists() else "!!", "data profile", "present" if paths.profile.exists() else "run data_inspector.py")
    db_ok = paths.db.exists()
    add("OK" if db_ok else "!!", "database file", f"{paths.db.stat().st_size / 1e6:,.0f} MB" if db_ok else str(paths.db))
    views, ingest_ok = {}, False
    if db_ok:
        try:
            with db.db_connection(paths.db, read_only=True) as con:
                if db.table_exists(con, "meta_ingestion_files"):
                    done, total = con.execute("SELECT count(*) FILTER (WHERE status = 'completed'), count(*) "
                                              "FROM meta_ingestion_files").fetchone()
                    rows_total = sum(db.row_count(con, t) for t in db.list_raw_tables(con))
                    ingest_ok = total > 0 and done == total and total >= len(jsonl)
                    add("OK" if ingest_ok else "!!", "ingestion", f"{done}/{total} files completed, {rows_total:,} rows")
                else:
                    add("!!", "ingestion", "not started - run ingestion.py")
                views = am.approved_views(con)
        except duckdb.IOException:
            add("!!", "database access", "locked by another process (backend holds a read lock - that's fine)")
    add("OK" if paths.template.exists() else "--", "schema mapping template",
        "present" if paths.template.exists() else "created by: recovery.py validate")
    add("OK" if paths.validation.exists() else "--", "relationship validation",
        "present" if paths.validation.exists() else "created by: recovery.py validate")
    add("OK" if views else "--", "typed views", f"{len(views)} approved views" if views else "created by: recovery.py prepare-demo")
    confirmed = []
    if paths.mapping.exists():
        try:
            m = load_json_report(paths.mapping, what="Analytics mapping")
            confirmed = [n for n, e in m.get("entities", {}).items() if isinstance(e, dict) and e.get("confirmed") is True]
        except ReportError as exc:
            add("!!", "analytics mapping", str(exc))
    add("OK" if confirmed else "--", "analytics mapping",
        f"confirmed entities: {', '.join(confirmed)}" if confirmed else "created by: recovery.py prepare-demo")

    ollama = _port_open(11434)
    model_ok = False
    if ollama:
        try:
            names = [x.get("name") for x in _local_get("http://127.0.0.1:11434/api/tags").get("models", [])]
            from llm_service import OllamaClient
            model_ok = any(OllamaClient(model=config.OLLAMA_MODEL).model_matches(n) for n in names if n)
        except Exception:
            pass
    add("OK" if ollama else "--", "Ollama (127.0.0.1:11434)", "listening" if ollama else "not running")
    add("OK" if model_ok else "--", f"model {config.OLLAMA_MODEL}",
        "installed" if model_ok else "not installed or BLAST_OLLAMA_MODEL not set (chat uses template answers)")
    if _port_open(config.API_PORT):
        try:
            h = _local_get(f"http://127.0.0.1:{config.API_PORT}/api/health")
            ai = _local_get(f"http://127.0.0.1:{config.API_PORT}/api/ai/status")
            add("OK" if h.get("status") == "ok" else "!!", "backend /api/health",
                f"{h.get('status')} - entities {h.get('analytics_mapping', {}).get('confirmed_entities')}")
            add("OK" if ai.get("model_available") else "--", "backend /api/ai/status",
                f"reachable={ai.get('reachable')} model={ai.get('model')} available={ai.get('model_available')}")
        except Exception:
            add("!!", "backend", f"port {config.API_PORT} open but health check failed")
    else:
        add("--", "backend (127.0.0.1:8000)", "not running")
    add("OK" if _port_open(5173) else "--", "frontend (127.0.0.1:5173)", "listening" if _port_open(5173) else "not running")

    for status, item, detail in rows:
        print(f"[{status}] {item:<32} {detail}")
    if not ingest_ok:
        nxt = "finish ingestion: python backend\\ingestion.py"
    elif not paths.validation.exists():
        nxt = "python backend\\recovery.py validate"
    elif not views or not confirmed:
        nxt = "python backend\\recovery.py prepare-demo"
    else:
        nxt = "start the app: powershell -ExecutionPolicy Bypass -File scripts\\start-local.ps1"
    print(f"\nNext step: {nxt}")
    return 0 if ingest_ok and views and confirmed else 1


def test_stage(skip_frontend: bool) -> int:
    results = []
    print("Backend synthetic tests ...")
    rc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(BACKEND_DIR / "tests")],
                        cwd=PROJECT_ROOT).returncode
    results.append(("backend tests", rc))
    npm = shutil.which("npm")
    if not skip_frontend:
        if npm is None:
            results.append(("frontend (npm not found)", 1))
        else:
            fe = PROJECT_ROOT / "frontend"
            if not (fe / "node_modules").exists():
                results.append(("npm install", subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=fe).returncode))
            results.append(("frontend tests", subprocess.run([npm, "test"], cwd=fe).returncode))
            results.append(("frontend build", subprocess.run([npm, "run", "build"], cwd=fe).returncode))
    print()
    for name, code in results:
        print(f"[{'OK' if code == 0 else 'FAIL'}] {name}")
    return 0 if all(c == 0 for _, c in results) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def write_recovery_report(paths: Paths, stage: str, summary: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if paths.report.exists():
        try:
            existing = load_json_report(paths.report, what="Recovery report")
        except ReportError:
            existing = {}
    existing.update({"generated_at_utc": _now(), "privacy": "Counts, ids, reason codes and field names only. "
                                                           "No record values.", stage: summary})
    write_json_atomic(paths.report, existing)
    lines = ["# Recovery report", "", f"_Updated {existing['generated_at_utc']}. Local only._", ""]
    v = existing.get("validate")
    if v:
        lines += ["## Relationship decisions (strict automatic policy)", "",
                  "| id | from | to | decision | reasons | matched | orphans | cardinality |", "|---|---|---|---|---|---|---|---|"]
        for d in v["decisions"]:
            e, f, t = d["evidence"], d.get("from") or {}, d.get("to") or {}
            lines.append(f"| {d['id']} | {f.get('dataset')}.{f.get('field')} | {t.get('dataset')}.{t.get('field')} | "
                         f"{d['decision']} | {', '.join(d['reasons']) or '-'} | {e.get('matched_values', '-')} | "
                         f"{e.get('orphan_records', '-')} | {e.get('observed_cardinality', '-')} |")
        lines += ["", f"Primary keys: {v['primary_keys']}", ""]
    p = existing.get("prepare-demo")
    if p:
        lines += ["## Demo preparation", "", f"Typed views: {p['typed_views']}", "", "### Analytics entities", ""]
        for ent, s in p["analytics_mapping"]["entities"].items():
            lines.append(f"- **{ent}**: " + (f"confirmed; roles {', '.join(s['roles'])}; unavailable roles "
                                             f"{', '.join(s['roles_unavailable']) or '-'}" if s["confirmed"]
                                             else f"not confirmed - {s['reason']}"))
        lines += ["", "### Unavailable metrics", ""]
        lines += [f"- {a['title']}: {a.get('reason') or '-'}" for a in p["analytics"]["availability"] if a["status"] != "ok"]
    write_text_atomic(paths.report.with_suffix(".md"), "\n".join(lines) + "\n")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local recovery and demo orchestration (strict automatic decisions).")
    p.add_argument("command", choices=["check", "validate", "prepare-demo", "test"])
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--reports-dir", type=Path, default=config.REPORTS_DIR)
    p.add_argument("--salt-file", type=Path, default=config.LABEL_SALT_PATH)
    p.add_argument("--models-dir", type=Path, default=config.MODELS_DIR)
    p.add_argument("--skip-frontend", action="store_true", help="test: backend tests only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    paths = Paths(args.db.resolve(), args.data_dir.resolve(), args.reports_dir.resolve(), args.salt_file.resolve(),
                  args.models_dir.resolve())
    policy = Policy()
    try:
        if args.command == "check":
            return check_stage(paths)
        if args.command == "test":
            return test_stage(args.skip_frontend)
        paths.reports.mkdir(parents=True, exist_ok=True)
        if args.command == "validate":
            write_recovery_report(paths, "validate", validate_stage(paths, policy))
        else:
            summary = prepare_stage(paths, policy)
            write_recovery_report(paths, "prepare-demo", summary)
        print(f"\nDetails: {paths.report.with_suffix('.md')}")
        return 0
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except duckdb.Error as exc:
        print(f"ERROR: {_db_failure(args.command, exc, 'files of the failing step were not written')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
