"""
Blast & drilling analytics engine (local DuckDB, approved typed views only).

Every metric is a dict produced from a real SQL query and carries:
``key``, ``title``, ``status`` (ok | unavailable | suppressed | error),
``value``/``details``, ``unit``, ``method`` (how it was calculated),
``sources`` (view, dataset and columns used), ``sample_size`` and
``missing`` counts. Nothing is estimated or invented: if the mapping lacks a
required role, or a filter cannot be applied, the metric is ``unavailable``
with the reason.

Privacy
-------
* Reads only views registered by typed_views.py; never raw data or data/.
* Group labels (rig, site, blast, problem category) are pseudonymised by
  default (``RIG-3fa9c1``: salted SHA-256 computed inside DuckDB, salt kept in
  local_db/). ``--reveal-labels`` shows real labels in LOCAL outputs only.
* Groups smaller than ``privacy.min_group_size`` (default 5) are suppressed.
* No minimum/maximum or individual record values are reported; anomalies are
  referenced by lineage (``_record_key``, source file, line) and explained
  with robust z-scores and percentiles only.

CLI
---
    python backend/analytics.py suggest-mapping
    python backend/analytics.py run [--from 2024-01-01] [--to 2024-06-30] [--rig X] [--site Y] [--blast Z]
                                    [--compare-blast ID ...] [--grain month|week] [--train] [--reveal-labels]
    python backend/analytics.py readiness
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402

import anomaly_detection  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402
import predictions  # noqa: E402
from analytics_mapping import (  # noqa: E402
    DURATION_UNITS, EntityMap, ResolvedMapping, approved_views, resolve_mapping, suggest_mapping,
)
from logging_setup import safe_error  # noqa: E402
from report_io import ReportError, confirm_overwrite, load_json_report, write_json_atomic, write_text_atomic  # noqa: E402

ANALYTICS_VERSION = "1.0"
MAPPING_PATH = config.REPORTS_DIR / "analytics_mapping.json"
JSON_REPORT = "analytics_summary.json"
MD_REPORT = "analytics_summary.md"
SALT_PATH = config.DB_DIR / "label_salt.txt"
TIME_ROLES = {"blasts": ("time",), "holes": ("start_time", "end_time"), "problems": ("time",)}
DURATION_BUCKETS = ((0, 30, "<30 min"), (30, 60, "30-60 min"), (60, 120, "1-2 h"), (120, 240, "2-4 h"),
                    (240, 480, "4-8 h"), (480, None, ">8 h"))
UTILIZATION_MIN_SPAN_HOURS = 24.0
GRAINS = {"month": "%Y-%m", "week": "%G-W%V"}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Filters:
    date_from: date | None = None
    date_to: date | None = None
    rigs: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    blasts: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        """Filter description without echoing identifier values."""
        return {
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "rig_values": len(self.rigs), "site_values": len(self.sites), "blast_values": len(self.blasts),
        }

    @property
    def active(self) -> bool:
        return bool(self.date_from or self.date_to or self.rigs or self.sites or self.blasts)


class FilterNotApplicable(Exception):
    """A requested filter cannot be applied to an entity (role not mapped)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _r(x: Any, nd: int = 3) -> Any:
    return round(float(x), nd) if isinstance(x, (int, float)) and x is not None else x


def _pct(n: Any, d: Any) -> float | None:
    return round(100.0 * n / d, 2) if n is not None and d else None


def metric(key: str, title: str, *, method: str = "", sources: list[dict] | None = None, value: Any = None,
           unit: str | None = None, sample_size: int | None = None, missing: dict | None = None,
           status: str = "ok", reason: str | None = None, details: Any = None, sample_meaning: str | None = None,
           limitations: list[str] | None = None) -> dict[str, Any]:
    """``sample_size`` is always the number of observations the value was computed from;
    ``sample_meaning`` says what those observations are (e.g. "holes with a valid duration")."""
    return {"key": key, "title": title, "status": status, "value": value, "unit": unit, "method": method,
            "sources": sources or [], "sample_size": sample_size, "sample_meaning": sample_meaning,
            "missing": missing or {}, "reason": reason, "details": details, "limitations": limitations or []}


def count_limitations(label: str, counts: dict[str, int], has_pairs: bool) -> list[str]:
    """Explain when record counts and distinct-entity counts differ (never present them as one number)."""
    out = []
    rows, ids, distinct = counts.get("filtered_rows", 0), counts.get("non_null_ids", 0), counts.get("distinct_ids", 0)
    if distinct and ids > distinct:
        out.append(f"{label.capitalize()} IDs repeat across records (on average {ids / distinct:.1f} records per ID): "
                   f"the view is not one row per {label}, so record counts are not {label} counts. The distinct count "
                   f"assumes the ID is unique across the whole dataset; if it is only unique within a parent "
                   f"(e.g. a blast or field) it undercounts {label}s"
                   + (" - compare the distinct (blast ID, hole ID) pairs." if has_pairs else "."))
    if rows > ids:
        out.append(f"{rows - ids:,} records have no {label} ID and are not part of the distinct count.")
    return out


def unavailable(key: str, title: str, reason: str) -> dict[str, Any]:
    return metric(key, title, status="unavailable", reason=reason)


def safe_metric(key: str, title: str) -> Callable:
    """Turn filter/database problems into an 'unavailable'/'error' metric instead of crashing."""
    def deco(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(self: "AnalyticsEngine", *a: Any, **kw: Any) -> dict[str, Any]:
            try:
                return fn(self, *a, **kw)
            except FilterNotApplicable as exc:
                return unavailable(key, title, str(exc))
            except duckdb.Error as exc:
                if self.debug:
                    traceback.print_exc()
                return metric(key, title, status="error", reason=f"query failed ({safe_error(exc)})")
        return wrapper
    return deco


def load_or_create_salt(path: Path) -> str:
    if path.exists():
        salt = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{32}", salt):
            return salt
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(16)
    path.write_text(salt, encoding="utf-8")
    return salt


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class AnalyticsEngine:
    """Reusable analytics over approved typed views. Methods return JSON-serialisable dicts."""

    def __init__(self, con: duckdb.DuckDBPyConnection, mapping: ResolvedMapping, *, salt: str,
                 reveal_labels: bool = False, debug: bool = False) -> None:
        if not re.fullmatch(r"[0-9a-f]{16,64}", salt):
            raise ValueError("invalid label salt")
        self.con, self.m, self.salt, self.debug = con, mapping, salt, debug
        self.k = mapping.min_group_size
        self.reveal = reveal_labels or not mapping.pseudonymize
        self.view_dataset: dict[str, str] = {}
        if db.table_exists(con, "meta_typed_views"):
            self.view_dataset = dict(con.execute("SELECT view_name, any_value(dataset) FROM meta_typed_views "
                                                 "GROUP BY view_name").fetchall())

    # -- building blocks ------------------------------------------------------
    @property
    def blasts(self) -> EntityMap | None:
        return self.m.get("blasts")

    @property
    def holes(self) -> EntityMap | None:
        return self.m.get("holes")

    @property
    def problems(self) -> EntityMap | None:
        return self.m.get("problems")

    def label(self, col_sql: str, prefix: str) -> str:
        """SQL expression for a group label: pseudonym by default, real value only with reveal."""
        if self.reveal:
            return f"CAST({col_sql} AS VARCHAR)"
        return f"('{prefix}-' || left(sha256('{self.salt}' || CAST({col_sql} AS VARCHAR)), 6))"

    def source(self, ent: EntityMap, *roles: str, columns: list[str] | None = None) -> dict[str, Any]:
        cols = [ent.columns[r] for r in roles if r in ent.columns] + (columns or [])
        return {"view": ent.view, "dataset": self.view_dataset.get(ent.view), "columns": cols}

    def time_role(self, ent: EntityMap) -> str | None:
        return next((r for r in TIME_ROLES[ent.name] if ent.has(r)), None)

    def where(self, ent: EntityMap, f: Filters, extra: list[str] | None = None) -> tuple[str, list[Any]]:
        clauses, params = list(extra or []), []
        if f.date_from or f.date_to:
            tr = self.time_role(ent)
            if not tr:
                raise FilterNotApplicable(f"date filter cannot be applied to {ent.name}: no time column mapped")
            if f.date_from:
                clauses.append(f"{ent.q(tr)} >= CAST(? AS TIMESTAMP)")
                params.append(f.date_from.isoformat())
            if f.date_to:
                clauses.append(f"{ent.q(tr)} < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY")
                params.append(f.date_to.isoformat())
        for role, values in (("rig", f.rigs), ("site", f.sites), ("blast_id", f.blasts)):
            if not values:
                continue
            if not ent.has(role):
                raise FilterNotApplicable(f"{role} filter cannot be applied to {ent.name}: no {role} column mapped")
            clauses.append(f"CAST({ent.q(role)} AS VARCHAR) IN ({', '.join('?' * len(values))})")
            params.extend(values)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def one(self, sql: str, params: list[Any]) -> tuple:
        return self.con.execute(sql, params).fetchone()

    def rows(self, sql: str, params: list[Any]) -> list[tuple]:
        return self.con.execute(sql, params).fetchall()

    def view(self, ent: EntityMap) -> str:
        return db.quote_ident(ent.view)

    def duration_expr(self, h: EntityMap) -> tuple[str, str, list[str]] | None:
        """(SQL minutes expression, method text, columns) or None."""
        if h.has("duration"):
            factor = DURATION_UNITS[h.duration_unit]
            return (f"(CAST({h.q('duration')} AS DOUBLE) * {factor})",
                    f"mapped duration column ({h.duration_unit}) converted to minutes", [h.columns["duration"]])
        if h.has("start_time", "end_time"):
            return (f"(date_diff('second', {h.q('start_time')}, {h.q('end_time')}) / 60.0)",
                    "end_time minus start_time, in minutes", [h.columns["start_time"], h.columns["end_time"]])
        return None

    def completion_exprs(self, h: EntityMap) -> tuple[str, str, str] | None:
        """(completed SQL, known SQL, method) or None."""
        if not h.has("status"):
            return None
        s = h.q("status")
        if h.type_of("status") == "BOOLEAN":
            return f"({s} IS TRUE)", f"({s} IS NOT NULL)", "share of holes whose boolean status is true"
        if h.completed_values:
            vals = ", ".join(_sql_str(v) for v in h.completed_values)
            return (f"(CAST({s} AS VARCHAR) IN ({vals}))", f"({s} IS NOT NULL)",
                    f"share of holes whose status is one of the {len(h.completed_values)} configured completed values")
        return None

    def suppressed(self, n: Any) -> bool:
        return n is None or n < self.k

    # -- overview ---------------------------------------------------------------
    @safe_metric("total_blasts", "Total blasts")
    def total_blasts(self, f: Filters) -> dict[str, Any]:
        b, h = self.blasts, self.holes

        def distinct_metric(ent: EntityMap, method: str, missing_key: str, meaning: str) -> dict[str, Any]:
            w, p = self.where(ent, f)
            bid = ent.q("blast_id")
            rows, with_id, distinct = (int(x) for x in self.one(
                f"SELECT count(*), count({bid}), count(DISTINCT {bid}) FROM {self.view(ent)}{w}", p))
            total = int(self.one(f"SELECT count(*) FROM {self.view(ent)}", [])[0]) if f.active else rows
            counts = {"total_rows": total, "filtered_rows": rows, "non_null_ids": with_id, "distinct_ids": distinct}
            lim = count_limitations("blast", counts, False) if ent.name == "blasts" else (
                [f"{rows - with_id:,} hole records have no blast ID."] if rows > with_id else [])
            return metric("total_blasts", "Blasts (distinct blast IDs)", value=distinct, unit="distinct blast IDs",
                          method=method, sources=[self.source(ent, "blast_id")], sample_size=with_id,
                          sample_meaning=meaning, missing={missing_key: rows - with_id}, details={"counts": counts},
                          limitations=lim)

        if b:
            if b.has("blast_id"):
                return distinct_metric(b, "COUNT(DISTINCT blast_id) over the blasts view", "rows_without_blast_id",
                                       "blast records with a non-null blast ID")
            w, p = self.where(b, f)
            (rows,) = self.one(f"SELECT count(*) FROM {self.view(b)}{w}", p)
            return metric("total_blasts", "Blast records", value=int(rows), unit="records (rows)",
                          method="COUNT(*) of blast rows (no blast_id mapped)", sources=[self.source(b)],
                          sample_size=int(rows), sample_meaning="blast records (rows)",
                          limitations=["No blast ID is mapped: this is the number of records, not necessarily of blasts."])
        if h and h.has("blast_id"):
            return distinct_metric(h, "COUNT(DISTINCT blast_id) referenced by hole records (no blasts entity confirmed)",
                                   "holes_without_blast_id", "hole records with a non-null blast ID")
        return unavailable("total_blasts", "Total blasts", "no confirmed blasts entity and no holes.blast_id mapped")

    @safe_metric("total_holes", "Total drilled holes")
    def total_holes(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        if not h:
            return unavailable("total_holes", "Total drilled holes", "holes entity not confirmed")
        w, p = self.where(h, f)
        total_rows = int(self.one(f"SELECT count(*) FROM {self.view(h)}", [])[0]) if f.active else None
        if h.has("hole_id"):
            hid = h.q("hole_id")
            pair_sql = (f", count(DISTINCT concat_ws(chr(31), CAST({h.q('blast_id')} AS VARCHAR), CAST({hid} AS VARCHAR))) "
                        f"FILTER (WHERE {hid} IS NOT NULL AND {h.q('blast_id')} IS NOT NULL)") if h.has("blast_id") else ""
            row = self.one(f"SELECT count(*), count({hid}), count(DISTINCT {hid}){pair_sql} FROM {self.view(h)}{w}", p)
            rows, with_id, distinct = (int(x) for x in row[:3])
            counts = {"total_rows": total_rows if total_rows is not None else rows, "filtered_rows": rows,
                      "non_null_ids": with_id, "distinct_ids": distinct}
            if pair_sql:
                counts["distinct_blast_hole_pairs"] = int(row[3])
            return metric("total_holes", "Drilled holes (distinct hole IDs)", value=distinct, unit="distinct hole IDs",
                          method="COUNT(DISTINCT hole_id) over the holes view; record rows, non-null IDs and distinct "
                                 "IDs are reported as separate counts",
                          sources=[self.source(h, "hole_id", "blast_id")], sample_size=with_id,
                          sample_meaning="hole records with a non-null hole ID",
                          missing={"rows_without_hole_id": rows - with_id}, details={"counts": counts},
                          limitations=count_limitations("hole", counts, bool(pair_sql)))
        (rows,) = self.one(f"SELECT count(*) FROM {self.view(h)}{w}", p)
        counts = {"total_rows": total_rows if total_rows is not None else int(rows), "filtered_rows": int(rows)}
        return metric("total_holes", "Hole records", value=int(rows), unit="records (rows)",
                      method="COUNT(*) of hole rows (no hole_id mapped)", sources=[self.source(h)],
                      sample_size=int(rows), sample_meaning="hole records (rows)", details={"counts": counts},
                      limitations=["No hole ID is mapped: this is the number of records, which is not necessarily "
                                   "the number of drilled holes."])

    @safe_metric("completion_rate", "Drilling completion rate")
    def completion_rate(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        comp = self.completion_exprs(h) if h else None
        if not comp:
            return unavailable("completion_rate", "Drilling completion rate",
                               "needs holes.status (BOOLEAN) or holes.status + completed_values in the mapping")
        done, known, method = comp
        w, p = self.where(h, f)
        rows, n_known, n_done = self.one(
            f"SELECT count(*), count(*) FILTER (WHERE {known}), count(*) FILTER (WHERE {done}) FROM {self.view(h)}{w}", p)
        if self.suppressed(n_known):
            return metric("completion_rate", "Drilling completion rate", status="suppressed", sample_size=int(n_known),
                          reason=f"fewer than {self.k} holes with a known status", sources=[self.source(h, "status")])
        return metric("completion_rate", "Drilling completion rate", value=_pct(n_done, n_known), unit="%",
                      method=method + "; denominator = holes with a non-null status",
                      sources=[self.source(h, "status")], sample_size=int(n_known), sample_meaning="hole records with a known status",
                      missing={"holes_without_status": int(rows - n_known)},
                      details={"completed_holes": int(n_done)})

    @safe_metric("problem_frequency", "Frequency of recorded drilling problems")
    def problem_frequency(self, f: Filters) -> dict[str, Any]:
        pr, h = self.problems, self.holes
        if pr:
            w, p = self.where(pr, f)
            (total,) = self.one(f"SELECT count(*) FROM {self.view(pr)}{w}", p)
            details: dict[str, Any] = {"problem_records": int(total)}
            sources = [self.source(pr, "category")]
            holes_total = None
            if h:
                try:
                    hw, hp = self.where(h, f)
                    (holes_total,) = self.one(f"SELECT count(*) FROM {self.view(h)}{hw}", hp)
                    sources.append(self.source(h))
                except FilterNotApplicable as exc:
                    details["per_100_holes_note"] = str(exc)
            if pr.has("category"):
                cats = self.rows(
                    f"SELECT {self.label(pr.q('category'), 'CAT')} AS c, count(*) AS n FROM {self.view(pr)}{w} "
                    f"GROUP BY c ORDER BY n DESC", p)
                shown = [{"category": c, "records": int(n), "share_pct": _pct(n, total)} for c, n in cats if n >= self.k]
                details["by_category"] = shown
                details["categories_suppressed"] = sum(1 for _, n in cats if n < self.k)
            return metric("problem_frequency", "Frequency of recorded drilling problems",
                          value=_pct(total, holes_total) if holes_total else int(total),
                          unit="problems per 100 holes" if holes_total else "problem records",
                          method="COUNT(*) of problem records" + (" divided by hole rows x 100 (same filters)"
                                                                  if holes_total else ""),
                          sources=sources, sample_size=int(total), details=details, sample_meaning="problem records")
        if h and h.has("problem_flag"):
            w, p = self.where(h, f)
            rows, known, flagged = self.one(
                f"SELECT count(*), count({h.q('problem_flag')}), count(*) FILTER (WHERE {h.q('problem_flag')}) "
                f"FROM {self.view(h)}{w}", p)
            if self.suppressed(known):
                return metric("problem_frequency", "Frequency of recorded drilling problems", status="suppressed",
                              sample_size=int(known), reason=f"fewer than {self.k} holes with a problem flag")
            return metric("problem_frequency", "Frequency of recorded drilling problems",
                          value=_pct(flagged, known), unit="% of holes flagged",
                          method="holes with problem_flag = true / holes with a non-null flag",
                          sources=[self.source(h, "problem_flag")], sample_size=int(known), sample_meaning="hole records with a non-null problem flag",
                          missing={"holes_without_flag": int(rows - known)})
        return unavailable("problem_frequency", "Frequency of recorded drilling problems",
                           "no confirmed problems entity and no holes.problem_flag mapped")

    # -- drilling performance ----------------------------------------------------
    @safe_metric("drilling_duration", "Drilling duration")
    def drilling_duration(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        d = self.duration_expr(h) if h else None
        if not d:
            return unavailable("drilling_duration", "Drilling duration",
                               "needs holes.duration or both holes.start_time and holes.end_time")
        expr, method, cols = d
        w, p = self.where(h, f)
        rows, known, valid, invalid, mean, med, p90, sd = self.one(
            f"SELECT count(*), count({expr}), count(*) FILTER (WHERE {expr} > 0), count(*) FILTER (WHERE {expr} <= 0), "
            f"avg({expr}) FILTER (WHERE {expr} > 0), median({expr}) FILTER (WHERE {expr} > 0), "
            f"quantile_cont({expr}, 0.9) FILTER (WHERE {expr} > 0), stddev_samp({expr}) FILTER (WHERE {expr} > 0) "
            f"FROM {self.view(h)}{w}", p)
        base = dict(sources=[self.source(h, columns=cols)], sample_size=int(valid), sample_meaning="hole records with a valid duration (> 0)",
                    missing={"holes_without_duration": int(rows - known), "non_positive_durations_excluded": int(invalid)})
        if self.suppressed(valid):
            return metric("drilling_duration", "Drilling duration", status="suppressed",
                          reason=f"fewer than {self.k} valid durations", method=method, **base)
        return metric("drilling_duration", "Drilling duration", value=_r(mean, 2), unit="minutes (mean)",
                      method=f"{method}; durations <= 0 excluded", **base,
                      details={"mean_minutes": _r(mean, 2), "median_minutes": _r(med, 2),
                               "p90_minutes": _r(p90, 2), "stddev_minutes": _r(sd, 2)})

    @safe_metric("drilled_depth", "Drilled depth")
    def drilled_depth(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        if not (h and h.has("depth")):
            return unavailable("drilled_depth", "Drilled depth", "holes.depth not mapped")
        dcol = h.q("depth")
        w, p = self.where(h, f)
        rows, known, valid, total, mean, med = self.one(
            f"SELECT count(*), count({dcol}), count(*) FILTER (WHERE {dcol} > 0), sum({dcol}) FILTER (WHERE {dcol} > 0), "
            f"avg({dcol}) FILTER (WHERE {dcol} > 0), median({dcol}) FILTER (WHERE {dcol} > 0) FROM {self.view(h)}{w}", p)
        base = dict(sources=[self.source(h, "depth")], sample_size=int(valid), sample_meaning="hole records with depth > 0",
                    missing={"holes_without_depth": int(rows - known), "non_positive_depth_excluded": int(known - valid)})
        if self.suppressed(valid):
            return metric("drilled_depth", "Drilled depth", status="suppressed",
                          reason=f"fewer than {self.k} valid depths", **base)
        return metric("drilled_depth", "Drilled depth", value=_r(total, 2), unit="depth units (total, as stored)",
                      method="SUM/AVG/MEDIAN of depth > 0", **base,
                      details={"mean_depth": _r(mean, 3), "median_depth": _r(med, 3)})

    @safe_metric("drilling_rate", "Drilling rate")
    def drilling_rate(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        d = self.duration_expr(h) if h else None
        if not (h and d and h.has("depth")):
            return unavailable("drilling_rate", "Drilling rate", "needs holes.depth and a drilling duration")
        expr, method, cols = d
        rate = f"({h.q('depth')} / ({expr} / 60.0))"
        w, p = self.where(h, f, [f"{expr} > 0", f"{h.q('depth')} > 0"])
        n, med, mean = self.one(f"SELECT count(*), median({rate}), avg({rate}) FROM {self.view(h)}{w}", p)
        src = [self.source(h, "depth", columns=cols)]
        if self.suppressed(n):
            return metric("drilling_rate", "Drilling rate", status="suppressed", sample_size=int(n), sources=src,
                          reason=f"fewer than {self.k} holes with both depth and duration")
        return metric("drilling_rate", "Drilling rate", value=_r(med, 3), unit="depth units per hour (median)",
                      method=f"depth / ({method} / 60) per hole, holes with depth > 0 and duration > 0",
                      sources=src, sample_size=int(n), details={"mean_rate": _r(mean, 3)}, sample_meaning="hole records with depth > 0 and duration > 0")

    @safe_metric("completion_time_analysis", "Drilling completion time analysis")
    def completion_time_analysis(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        if not (h and h.has("start_time", "end_time")):
            return unavailable("completion_time_analysis", "Drilling completion time analysis",
                               "requires valid holes.start_time AND holes.end_time timestamps")
        s, e = h.q("start_time"), h.q("end_time")
        mins = f"(date_diff('second', {s}, {e}) / 60.0)"
        w, p = self.where(h, f)
        rows, both, valid, invalid = self.one(
            f"SELECT count(*), count(*) FILTER (WHERE {s} IS NOT NULL AND {e} IS NOT NULL), "
            f"count(*) FILTER (WHERE {mins} > 0), count(*) FILTER (WHERE {mins} <= 0) FROM {self.view(h)}{w}", p)
        cases = " ".join(f"WHEN {mins} >= {lo}" + (f" AND {mins} < {hi}" if hi else "") + f" THEN {i}"
                         for i, (lo, hi, _) in enumerate(DURATION_BUCKETS))
        wb, pb = self.where(h, f, [f"{mins} > 0"])
        buckets = dict(self.rows(f"SELECT CASE {cases} END AS b, count(*) FROM {self.view(h)}{wb} GROUP BY b", pb))
        dist = [{"bucket": label, "holes": (int(buckets.get(i, 0)) if buckets.get(i, 0) >= self.k else None),
                 "share_pct": (_pct(buckets.get(i, 0), valid) if buckets.get(i, 0) >= self.k else None),
                 "suppressed": 0 < buckets.get(i, 0) < self.k}
                for i, (_, _, label) in enumerate(DURATION_BUCKETS)]
        return metric("completion_time_analysis", "Drilling completion time analysis",
                      value=_pct(valid, both), unit="% of holes with a valid start->end interval",
                      method="end_time - start_time per hole; intervals <= 0 are invalid; bucketed distribution",
                      sources=[self.source(h, "start_time", "end_time")], sample_size=int(valid), sample_meaning="hole records with a valid start-to-end interval",
                      missing={"holes_missing_start_or_end": int(rows - both), "invalid_intervals": int(invalid)},
                      details={"distribution": dist})

    # -- rig and site --------------------------------------------------------------
    def _group_stats(self, h: EntityMap, f: Filters, role: str, prefix: str) -> tuple[list[dict], int, list[dict]]:
        d = self.duration_expr(h)
        dur = d[0] if d else "NULL::DOUBLE"
        depth = h.q("depth") if h.has("depth") else "NULL::DOUBLE"
        comp = self.completion_exprs(h)
        done, known = (comp[0], comp[1]) if comp else ("NULL::BOOLEAN", "FALSE")
        flag = f"({h.q('problem_flag')} IS TRUE)" if h.has("problem_flag") else "NULL::BOOLEAN"
        w, p = self.where(h, f, [f"{h.q(role)} IS NOT NULL"])
        sql = f"""
            WITH base AS (SELECT {self.label(h.q(role), prefix)} AS g, {dur} AS dur, {depth} AS depth,
                                 {done} AS done, {known} AS known, {flag} AS flag FROM {self.view(h)}{w})
            SELECT g, count(*), count(*) FILTER (WHERE dur > 0), median(dur) FILTER (WHERE dur > 0),
                   avg(depth) FILTER (WHERE depth > 0),
                   median(depth / (dur / 60.0)) FILTER (WHERE dur > 0 AND depth > 0),
                   count(*) FILTER (WHERE known), count(*) FILTER (WHERE done), count(*) FILTER (WHERE flag)
            FROM base GROUP BY g ORDER BY count(*) DESC"""
        out, suppressed = [], 0
        for g, n, n_dur, med, mean_depth, rate, n_known, n_done, n_flag in self.rows(sql, p):
            if n < self.k:
                suppressed += 1
                continue
            out.append({
                "group": g, "holes": int(n), "holes_with_duration": int(n_dur),
                "median_duration_minutes": _r(med, 2) if n_dur >= self.k else None,
                "mean_depth": _r(mean_depth, 3), "median_drilling_rate_per_hour": _r(rate, 3),
                "completion_rate_pct": _pct(n_done, n_known) if comp and n_known >= self.k else None,
                "problem_flag_rate_pct": _pct(n_flag, n) if h.has("problem_flag") else None,
            })
        cols = [c for c in (d[2] if d else [])] + [h.columns[r] for r in (role, "depth", "status", "problem_flag")
                                                   if r in h.columns]
        return out, suppressed, [self.source(h, columns=cols)]

    def _utilization(self, h: EntityMap, f: Filters) -> dict[str, dict[str, Any]]:
        s, e = h.q("start_time"), h.q("end_time")
        w, p = self.where(h, f, [f"{h.q('rig')} IS NOT NULL", f"{e} > {s}"])
        sql = f"""
            WITH iv AS (SELECT {self.label(h.q('rig'), 'RIG')} AS g, {s} AS s, {e} AS e FROM {self.view(h)}{w}),
            o AS (SELECT g, s, e, max(e) OVER (PARTITION BY g ORDER BY s, e
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end FROM iv),
            isl AS (SELECT g, s, e, sum(CASE WHEN prev_end IS NULL OR s > prev_end THEN 1 ELSE 0 END)
                    OVER (PARTITION BY g ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS grp FROM o),
            merged AS (SELECT g, grp, min(s) AS s, max(e) AS e FROM isl GROUP BY g, grp),
            cnt AS (SELECT g, count(*) AS n FROM iv GROUP BY g),
            busy AS (SELECT g, sum(date_diff('second', s, e)) / 3600.0 AS busy_h,
                            date_diff('second', min(s), max(e)) / 3600.0 AS span_h FROM merged GROUP BY g)
            SELECT busy.g, busy_h, span_h, n FROM busy JOIN cnt USING (g)"""
        out = {}
        for g, busy, span, n in self.rows(sql, p):
            ok = n >= self.k and span and span >= UTILIZATION_MIN_SPAN_HOURS
            out[g] = {"utilization_pct": _pct(busy, span) if ok else None,
                      "utilization_note": None if ok else
                      f"insufficient data (needs >= {self.k} intervals over >= {UTILIZATION_MIN_SPAN_HOURS:.0f} h)"}
        return out

    @safe_metric("rig_performance", "Rig performance")
    def rig_performance(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        if not (h and h.has("rig")):
            return unavailable("rig_performance", "Rig performance", "holes.rig not mapped")
        rows, suppressed, sources = self._group_stats(h, f, "rig", "RIG")
        method = ("per rig: hole count, median duration, mean depth, median drilling rate, completion rate, "
                  "problem-flag rate (holes view)")
        if h.has("start_time", "end_time"):
            util = self._utilization(h, f)
            for r in rows:
                r.update(util.get(r["group"], {"utilization_pct": None, "utilization_note": "no valid intervals"}))
            method += ("; utilization = merged drilling intervals / time between the rig's first start and last end "
                       f"(proxy: idle time outside recorded data unknown; needs >= {UTILIZATION_MIN_SPAN_HOURS:.0f} h span)")
        pr = self.problems
        if pr and pr.has("rig"):
            try:
                pw, pp = self.where(pr, f, [f"{pr.q('rig')} IS NOT NULL"])
                counts = dict(self.rows(f"SELECT {self.label(pr.q('rig'), 'RIG')} AS g, count(*) FROM {self.view(pr)}{pw} "
                                        "GROUP BY g", pp))
                for r in rows:
                    n = counts.get(r["group"], 0)
                    r["problems_per_100_holes"] = _pct(n, r["holes"]) if n == 0 or n >= self.k else None
                sources.append(self.source(pr, "rig"))
                method += "; problems per 100 holes joins problems.rig to holes.rig by identical value (assumed same format)"
            except FilterNotApplicable:
                pass
        return metric("rig_performance", "Rig performance", value=len(rows), unit="rigs reported",
                      method=method, sources=sources, sample_size=sum(r["holes"] for r in rows),
                      sample_meaning="hole records of the reported (non-suppressed) rigs",
                      details={"rigs": rows, "rigs_suppressed_small_sample": suppressed,
                               "labels": "real" if self.reveal else "pseudonymised"})

    @safe_metric("site_comparison", "Field/site comparison")
    def site_comparison(self, f: Filters) -> dict[str, Any]:
        h = self.holes
        if not (h and h.has("site")):
            return unavailable("site_comparison", "Field/site comparison", "holes.site not mapped")
        rows, suppressed, sources = self._group_stats(h, f, "site", "SITE")
        b = self.blasts
        if b and b.has("site"):
            try:
                bw, bp = self.where(b, f, [f"{b.q('site')} IS NOT NULL"])
                cnt = "count(DISTINCT " + b.q("blast_id") + ")" if b.has("blast_id") else "count(*)"
                counts = dict(self.rows(f"SELECT {self.label(b.q('site'), 'SITE')} AS g, {cnt} FROM {self.view(b)}{bw} "
                                        "GROUP BY g", bp))
                for r in rows:
                    n = counts.get(r["group"])
                    r["blasts"] = int(n) if n is not None and n >= self.k else None
                sources.append(self.source(b, "site", "blast_id"))
            except FilterNotApplicable:
                pass
        return metric("site_comparison", "Field/site comparison", value=len(rows), unit="sites reported",
                      method="per site: same hole statistics as rig performance; blasts per site from the blasts view",
                      sources=sources, sample_size=sum(r["holes"] for r in rows),
                      sample_meaning="hole records of the reported (non-suppressed) sites",
                      details={"sites": rows, "sites_suppressed_small_sample": suppressed,
                               "labels": "real" if self.reveal else "pseudonymised"})

    # -- blasts ------------------------------------------------------------------------
    def _per_blast_sql(self, f: Filters) -> tuple[str, list[Any], list[dict], list[str]] | None:
        """SQL producing one row per blast (blast label + metrics) - or None if no blast key exists."""
        h, b, pr = self.holes, self.blasts, self.problems
        parts, params, sources, metrics = [], [], [], []
        key = None
        if h and h.has("blast_id"):
            d = self.duration_expr(h)
            dur = d[0] if d else "NULL::DOUBLE"
            depth = h.q("depth") if h.has("depth") else "NULL::DOUBLE"
            w, p = self.where(h, f, [f"{h.q('blast_id')} IS NOT NULL"])
            parts.append(f"hs AS (SELECT CAST({h.q('blast_id')} AS VARCHAR) AS bid, count(*) AS holes, "
                         f"sum({depth}) FILTER (WHERE {depth} > 0) AS total_depth, "
                         f"median({dur}) FILTER (WHERE {dur} > 0) AS median_duration_minutes "
                         f"FROM {self.view(h)}{w} GROUP BY bid)")
            params += p
            sources.append(self.source(h, "blast_id", "depth"))
            metrics += ["holes", "total_depth", "median_duration_minutes"]
            key = "hs"
        if b and b.has("blast_id") and b.measures:
            w, p = self.where(b, f, [f"{b.q('blast_id')} IS NOT NULL"])
            ms = ", ".join(f"avg({db.quote_ident(m)}) AS {db.quote_ident('m_' + m)}" for m in b.measures)
            parts.append(f"bs AS (SELECT CAST({b.q('blast_id')} AS VARCHAR) AS bid, {ms} FROM {self.view(b)}{w} GROUP BY bid)")
            params += p
            sources.append(self.source(b, "blast_id", columns=list(b.measures)))
            metrics += ["m_" + m for m in b.measures]
            key = key or "bs"
        if pr and pr.has("blast_id"):
            w, p = self.where(pr, f, [f"{pr.q('blast_id')} IS NOT NULL"])
            parts.append(f"ps AS (SELECT CAST({pr.q('blast_id')} AS VARCHAR) AS bid, count(*) AS problems "
                         f"FROM {self.view(pr)}{w} GROUP BY bid)")
            params += p
            sources.append(self.source(pr, "blast_id"))
            metrics.append("problems")
        if key is None:
            return None
        ctes = [p_.split(" AS (")[0] for p_ in parts]
        allb = " UNION ".join(f"SELECT bid FROM {c}" for c in ctes)
        joins = " ".join(f"LEFT JOIN {c} USING (bid)" for c in ctes)
        sql = (f"WITH {', '.join(parts)}, allb AS ({allb}) "
               f"SELECT bid, {', '.join(metrics)} FROM allb {joins}")
        return sql, params, sources, metrics

    @safe_metric("blast_analysis", "Historical blast analysis")
    def blast_analysis(self, f: Filters) -> dict[str, Any]:
        built = self._per_blast_sql(f)
        if not built:
            return unavailable("blast_analysis", "Historical blast analysis",
                               "needs holes.blast_id, or blasts.blast_id with measures")
        sql, params, sources, metrics = built
        n_blasts = self.one(f"SELECT count(*) FROM ({sql})", params)[0]
        if self.suppressed(n_blasts):
            return metric("blast_analysis", "Historical blast analysis", status="suppressed", sample_size=int(n_blasts),
                          reason=f"fewer than {self.k} blasts", sources=sources)
        dist = {}
        for m in metrics:
            q = db.quote_ident(m)
            n, mean, med, p10, p90 = self.one(
                f"SELECT count({q}), avg({q}), median({q}), quantile_cont({q}, 0.1), quantile_cont({q}, 0.9) FROM ({sql})",
                params)
            dist[m] = ({"blasts_with_value": int(n), "mean": _r(mean), "median": _r(med), "p10": _r(p10), "p90": _r(p90)}
                       if n >= self.k else {"blasts_with_value": int(n), "suppressed": True})
        return metric("blast_analysis", "Historical blast analysis", value=int(n_blasts), unit="blasts analysed",
                      method="per-blast aggregates (holes per blast, total depth, median duration, blast measures, "
                             "problems), then distribution across blasts (mean/median/p10/p90)",
                      sources=sources, sample_size=int(n_blasts), details={"per_blast_distribution": dist}, sample_meaning="blasts with at least one aggregate")

    @safe_metric("blast_comparison", "Blast comparison")
    def compare_blasts(self, blast_ids: list[str], f: Filters) -> dict[str, Any]:
        if not blast_ids:
            return unavailable("blast_comparison", "Blast comparison", "no blast ids requested")
        built = self._per_blast_sql(f)
        if not built:
            return unavailable("blast_comparison", "Blast comparison", "no blast identifier mapped")
        sql, params, sources, metrics = built
        ranks = ", ".join(f"percent_rank() OVER (ORDER BY {db.quote_ident(m)}) AS {db.quote_ident('pr_' + m)}"
                          for m in metrics)
        ids = ", ".join("?" * len(blast_ids))
        q = (f"SELECT bid, {self.label('bid', 'BLAST')} AS lbl, {', '.join(metrics)}, {ranks}, count(*) OVER () AS n_all "
             f"FROM ({sql}) WHERE bid IS NOT NULL")
        found = {r[0]: r for r in self.rows(f"SELECT * FROM ({q}) WHERE bid IN ({ids})", params + list(blast_ids))}
        n_all = next(iter(found.values()))[-1] if found else 0
        out = []
        for i, bid in enumerate(blast_ids):
            r = found.get(bid)
            if r is None:
                out.append({"requested_index": i, "found": False})
                continue
            vals = r[2:2 + len(metrics)]
            prs = r[2 + len(metrics):2 + 2 * len(metrics)]
            row = {"requested_index": i, "found": True, "blast": r[1],
                   "percentile_vs_all_blasts": {m: (_r(100 * pr_, 1) if v is not None else None)
                                                for m, v, pr_ in zip(metrics, vals, prs)}}
            if self.reveal:
                row["values"] = {m: _r(v) for m, v in zip(metrics, vals)}
            else:
                row["values"] = {m: _r(v) for m, v in zip(metrics, vals) if m in ("holes", "problems")}
            out.append(row)
        return metric("blast_comparison", "Blast comparison", value=sum(1 for o in out if o["found"]),
                      unit="blasts compared", sources=sources, sample_size=int(n_all), sample_meaning="blasts in the comparison population",
                      method="per-blast aggregates and percentile rank among all blasts under the same filters; "
                             "blast-level values shown only with --reveal-labels",
                      details={"blasts": out, "not_found": sum(1 for o in out if not o["found"])})

    # -- trends ----------------------------------------------------------------------------
    @safe_metric("trends", "Historical performance trends")
    def trends(self, f: Filters, grain: str = "month") -> dict[str, Any]:
        fmt = GRAINS.get(grain)
        if not fmt:
            return unavailable("trends", "Historical performance trends", f"grain must be one of {sorted(GRAINS)}")
        series: dict[str, list[dict]] = {}
        sources = []
        h = self.holes
        if h and self.time_role(h):
            t = h.q(self.time_role(h))
            d = self.duration_expr(h)
            dur = d[0] if d else "NULL::DOUBLE"
            comp = self.completion_exprs(h)
            done, known = (comp[0], comp[1]) if comp else ("NULL::BOOLEAN", "FALSE")
            w, p = self.where(h, f, [f"{t} IS NOT NULL"])
            rows = self.rows(
                f"SELECT strftime(date_trunc('{grain}', {t}), '{fmt}') AS period, count(*), "
                f"median({dur}) FILTER (WHERE {dur} > 0), count(*) FILTER (WHERE {known}), "
                f"count(*) FILTER (WHERE {done}) FROM {self.view(h)}{w} GROUP BY period ORDER BY period", p)
            series["holes"] = [
                {"period": per, "holes": int(n), "median_duration_minutes": _r(med, 2),
                 "completion_rate_pct": _pct(dn, kn) if comp and kn >= self.k else None}
                if n >= self.k else {"period": per, "suppressed": True} for per, n, med, kn, dn in rows]
            sources.append(self.source(h, self.time_role(h), columns=d[2] if d else None))
        for ent, label in ((self.blasts, "blasts"), (self.problems, "problems")):
            if ent and self.time_role(ent):
                t = ent.q(self.time_role(ent))
                cnt = f"count(DISTINCT {ent.q('blast_id')})" if label == "blasts" and ent.has("blast_id") else "count(*)"
                w, p = self.where(ent, f, [f"{t} IS NOT NULL"])
                rows = self.rows(f"SELECT strftime(date_trunc('{grain}', {t}), '{fmt}') AS period, {cnt} "
                                 f"FROM {self.view(ent)}{w} GROUP BY period ORDER BY period", p)
                series[label] = [{"period": per, label: int(n)} if n >= self.k else {"period": per, "suppressed": True}
                                 for per, n in rows]
                sources.append(self.source(ent, self.time_role(ent)))
        if not series:
            return unavailable("trends", "Historical performance trends", "no time column mapped for any entity")
        return metric("trends", "Historical performance trends", value=sum(len(v) for v in series.values()),
                      unit=f"{grain} periods", sources=sources,
                      sample_size=sum(x.get("holes", 0) for x in series.get("holes", [])) or None, sample_meaning="hole records in non-suppressed periods",
                      method=f"counts and medians grouped by {grain} of each entity's time column; "
                             f"periods with fewer than {self.k} records suppressed",
                      details=series)

    # -- bundles -----------------------------------------------------------------------------
    def overview(self, f: Filters) -> list[dict[str, Any]]:
        return [self.total_blasts(f), self.total_holes(f), self.completion_rate(f), self.problem_frequency(f)]

    def drilling_performance(self, f: Filters) -> list[dict[str, Any]]:
        return [self.drilling_duration(f), self.drilled_depth(f), self.drilling_rate(f)]


# ---------------------------------------------------------------------------
# Full run + reports
# ---------------------------------------------------------------------------
def run_analytics(engine: AnalyticsEngine, f: Filters, *, grain: str = "month", compare: list[str] | None = None,
                  train: bool = False, models_dir: Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "analytics_version": ANALYTICS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": (f"Aggregates only; groups < {engine.k} suppressed; labels "
                    f"{'REAL (local only - do not share)' if engine.reveal else 'pseudonymised'}; "
                    "anomalies referenced by lineage, explained by robust z-scores/percentiles."),
        "min_group_size": engine.k,
        "labels": "real" if engine.reveal else "pseudonymised",
        "filters": f.describe(),
        "mapping_issues": engine.m.issues,
        "entities": {n: {"view": e.view, "dataset": engine.view_dataset.get(e.view), "roles": sorted(e.columns),
                         "measures": e.measures} for n, e in engine.m.entities.items()},
        "overview": engine.overview(f),
        "drilling_performance": engine.drilling_performance(f),
        "completion_time_analysis": engine.completion_time_analysis(f),
        "rig_performance": engine.rig_performance(f),
        "site_comparison": engine.site_comparison(f),
        "blast_analysis": engine.blast_analysis(f),
        "trends": engine.trends(f, grain),
    }
    if compare:
        report["blast_comparison"] = engine.compare_blasts(compare, f)
    report["anomalies"] = anomaly_detection.detect_all(engine, f)
    readiness = predictions.assess_readiness(engine, f)
    report["prediction"] = {"readiness": readiness}
    if train:
        report["prediction"]["baseline_model"] = predictions.train_baseline(
            engine, f, readiness, models_dir or config.MODELS_DIR)
    report["availability"] = availability(report)
    return report


def _metrics_in(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict) and "key" in obj and "status" in obj:
        return [obj]
    if isinstance(obj, list):
        return [m for x in obj for m in _metrics_in(x)]
    return []


def availability(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for section in ("overview", "drilling_performance", "completion_time_analysis", "rig_performance",
                    "site_comparison", "blast_analysis", "trends", "blast_comparison"):
        for m in _metrics_in(report.get(section)):
            out.append({"metric": m["key"], "title": m["title"], "status": m["status"], "reason": m.get("reason")})
    for d in report.get("anomalies", {}).get("detectors", []):
        out.append({"metric": f"anomaly:{d['detector']}", "title": d["title"], "status": d["status"],
                    "reason": d.get("reason")})
    r = report.get("prediction", {}).get("readiness", {})
    out.append({"metric": "prediction_readiness", "title": "Drilling-duration prediction readiness",
                "status": "ok" if r.get("ready") else "unavailable",
                "reason": None if r.get("ready") else "; ".join(r.get("reasons", []))})
    return out


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return str(v).replace("|", "/").replace("\n", " ")


def _md_metric(m: dict[str, Any]) -> list[str]:
    src = "; ".join(f"`{s['view']}` ({s.get('dataset') or '?'}): {', '.join(s['columns']) or 'row counts'}"
                    for s in m["sources"])
    lines = [f"### {m['title']}", "",
             f"- **Status:** {m['status']}" + (f" - {m['reason']}" if m.get("reason") else "")]
    if m["status"] == "ok":
        lines.append(f"- **Value:** {_cell(m['value'])} {m['unit'] or ''}")
    if m.get("method"):
        lines.append(f"- **Method:** {m['method']}")
    if src:
        lines.append(f"- **Sources:** {src}")
    if m.get("sample_size") is not None:
        lines.append(f"- **Sample size:** {_cell(m['sample_size'])}")
    if m.get("missing"):
        lines.append("- **Missing/excluded:** " + ", ".join(f"{k} {_cell(v)}" for k, v in m["missing"].items()))
    return lines + [""]


def _md_table(rows: list[dict[str, Any]], cols: list[str]) -> list[str]:
    cols = [c for c in cols if any(c in r for r in rows)]
    if not rows or not cols:
        return []
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
    return out + [""]


def render_markdown(r: dict[str, Any]) -> str:
    lines = ["# Analytics summary", "", f"_Generated {r['generated_at_utc']}. {r['privacy']}_", "",
             f"Filters: {', '.join(f'{k}={v}' for k, v in r['filters'].items() if v)}" if any(r['filters'].values())
             else "Filters: none", ""]
    if r["mapping_issues"]:
        lines += ["## Mapping issues", ""] + [f"- {_cell(i)}" for i in r["mapping_issues"]] + [""]
    lines += ["## Metric availability", ""]
    lines += _md_table(r["availability"], ["metric", "status", "reason"])
    lines += ["## Overview", ""]
    for m in r["overview"]:
        lines += _md_metric(m)
    lines += ["## Drilling performance", ""]
    for m in r["drilling_performance"]:
        lines += _md_metric(m)
        if m["status"] == "ok" and isinstance(m.get("details"), dict):
            lines += ["  " + ", ".join(f"{k} {_cell(v)}" for k, v in m["details"].items()), ""]
    m = r["completion_time_analysis"]
    lines += _md_metric(m)
    if m["status"] == "ok":
        lines += _md_table(m["details"]["distribution"], ["bucket", "holes", "share_pct", "suppressed"])
    for key, rows_key in (("rig_performance", "rigs"), ("site_comparison", "sites")):
        m = r[key]
        lines += _md_metric(m)
        if m["status"] == "ok":
            lines += _md_table(m["details"][rows_key], ["group", "holes", "median_duration_minutes", "mean_depth",
                                                         "median_drilling_rate_per_hour", "completion_rate_pct",
                                                         "problem_flag_rate_pct", "problems_per_100_holes",
                                                         "utilization_pct", "blasts"])
            lines += [f"Groups suppressed (< {r['min_group_size']} records): "
                      f"{m['details'][rows_key + '_suppressed_small_sample']}", ""]
    m = r["blast_analysis"]
    lines += _md_metric(m)
    if m["status"] == "ok":
        lines += _md_table([{"metric": k, **v} for k, v in m["details"]["per_blast_distribution"].items()],
                           ["metric", "blasts_with_value", "mean", "median", "p10", "p90", "suppressed"])
    if "blast_comparison" in r:
        m = r["blast_comparison"]
        lines += _md_metric(m)
        if m["status"] == "ok":
            for b in m["details"]["blasts"]:
                lines.append(f"- request #{b['requested_index']}: " + (
                    f"{b['blast']} - percentiles {b['percentile_vs_all_blasts']}" if b["found"] else "not found"))
            lines.append("")
    m = r["trends"]
    lines += _md_metric(m)
    if m["status"] == "ok":
        for series, rows in m["details"].items():
            lines += [f"**{series}**", ""] + _md_table(rows, ["period", "holes", "blasts", "problems",
                                                                "median_duration_minutes", "completion_rate_pct",
                                                                "suppressed"])
    a = r["anomalies"]
    lines += ["## Anomalies", "", f"_{a['label']}_", ""]
    lines += _md_table(a["detectors"], ["detector", "status", "records_scored", "flagged_count", "reason"])
    for d in a["detectors"]:
        for fl in d.get("flagged", [])[:10]:
            ref = fl.get("record") or {}
            where = (f"{ref.get('_source_file')} line {ref.get('_source_line')}" if ref else fl.get("group", ""))
            lines.append(f"- `{d['detector']}` {where}: {fl['explanation']}")
    lines.append("")
    pr = r["prediction"]["readiness"]
    lines += ["## Prediction readiness (drilling duration)", "",
              f"- **Ready:** {_cell(pr.get('ready'))}"] + [f"- {x}" for x in pr.get("reasons", [])]
    if pr.get("features"):
        lines += [""] + _md_table(pr["features"], ["feature", "kind", "missing_pct", "distinct_values", "usable", "note"])
    bm = r["prediction"].get("baseline_model")
    if bm:
        lines += ["### Baseline model", ""] + [f"- {k}: {_cell(v)}" for k, v in bm.items() if not isinstance(v, (dict, list))]
        if isinstance(bm.get("metrics"), dict):
            lines += _md_table([{"model": k, **v} for k, v in bm["metrics"].items()],
                               ["model", "mae", "rmse", "median_ae", "r2"])
    return "\n".join(lines) + "\n"


def print_summary(r: dict[str, Any]) -> None:
    print(f"\nAnalytics ({r['labels']} labels, groups < {r['min_group_size']} suppressed)")
    for i in r["mapping_issues"]:
        print(f"  note: {i}")
    print(f"\n{'metric':<34} {'status':<12} value")
    for m in r["overview"] + r["drilling_performance"] + [r["completion_time_analysis"], r["rig_performance"],
                                                          r["site_comparison"], r["blast_analysis"], r["trends"]]:
        val = f"{_cell(m['value'])} {m['unit'] or ''}" if m["status"] == "ok" else (m.get("reason") or "")
        print(f"{m['title'][:34]:<34} {m['status']:<12} {val[:90]}")
    a = r["anomalies"]
    print(f"\nAnomalies ({a['label']}):")
    for d in a["detectors"]:
        print(f"  {d['detector']:<28} {d['status']:<12} flagged={d.get('flagged_count', '-')}"
              + (f"  ({d['reason']})" if d.get("reason") else ""))
    pr = r["prediction"]["readiness"]
    print(f"\nPrediction readiness: {'READY' if pr.get('ready') else 'NOT READY'}")
    for x in pr.get("reasons", []):
        print(f"  - {x}")
    bm = r["prediction"].get("baseline_model")
    if bm:
        print(f"Baseline model: trained={bm.get('trained')}  {bm.get('verdict') or bm.get('reason') or ''}")
        for name, mm in (bm.get("metrics") or {}).items():
            print(f"  {name:<28} MAE={_cell(mm.get('mae'))}  RMSE={_cell(mm.get('rmse'))}  R2={_cell(mm.get('r2'))}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError("dates must be YYYY-MM-DD") from None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Blast & drilling analytics over approved typed views (local only).")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", type=Path, default=config.DB_PATH)
        sp.add_argument("--mapping", type=Path, default=MAPPING_PATH)
        sp.add_argument("--force", action="store_true", help="Overwrite outputs without asking (.bak kept).")
        sp.add_argument("--debug", action="store_true", help="Tracebacks on the console (may include values).")

    sg = sub.add_parser("suggest-mapping", help="Propose an analytics mapping from the approved typed views.")
    common(sg)
    for name in ("run", "readiness"):
        sp = sub.add_parser(name, help="Run all analytics" if name == "run" else "Prediction readiness only")
        common(sp)
        sp.add_argument("--from", dest="date_from", type=_date)
        sp.add_argument("--to", dest="date_to", type=_date)
        sp.add_argument("--rig", action="append", default=[])
        sp.add_argument("--site", action="append", default=[])
        sp.add_argument("--blast", action="append", default=[])
        sp.add_argument("--reveal-labels", action="store_true",
                        help="Show real rig/site/blast/category labels in LOCAL outputs (do not share them).")
        sp.add_argument("--salt-file", type=Path, default=SALT_PATH)
        if name == "run":
            sp.add_argument("--output-dir", type=Path, default=config.REPORTS_DIR)
            sp.add_argument("--grain", choices=sorted(GRAINS), default="month")
            sp.add_argument("--compare-blast", action="append", default=[], metavar="ID")
            sp.add_argument("--train", action="store_true", help="Train the baseline model if data is sufficient.")
            sp.add_argument("--models-dir", type=Path, default=config.MODELS_DIR)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        return 2
    try:
        with db.db_connection(db_path, read_only=True) as con:
            views = approved_views(con)
            if args.command == "suggest-mapping":
                if not views:
                    print("ERROR: no approved typed views found - run typed_views.py first.", file=sys.stderr)
                    return 2
                suggestion = suggest_mapping(views)
                target = args.mapping.resolve()
                if target.exists() and not args.force:
                    target = target.with_name(target.stem + ".suggested.json")
                    print(f"{args.mapping.name} exists - writing the suggestion to {target.name} instead.")
                write_json_atomic(target, suggestion)
                print(f"Wrote {target}\nReview it, set \"confirmed\": true per entity, then run: "
                      "python backend\\analytics.py run")
                for ent, e in suggestion["entities"].items():
                    mapped = sum(1 for v in e["columns"].values() if v)
                    print(f"  {ent:<9} view={e['view'] or '-':<36} roles suggested={mapped}/{len(e['columns'])}")
                return 0

            try:
                raw = load_json_report(args.mapping.resolve(), what="Analytics mapping")
                mapping = resolve_mapping(raw, views)
            except ReportError as exc:
                print(f"ERROR: {exc}\nCreate it with: python backend\\analytics.py suggest-mapping", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            if not mapping.entities:
                print("ERROR: no confirmed entities in the analytics mapping.", file=sys.stderr)
                for i in mapping.issues:
                    print(f"  - {i}", file=sys.stderr)
                return 2
            if args.reveal_labels:
                print("WARNING: --reveal-labels writes real identifiers into local reports. Do not share them.",
                      file=sys.stderr)
            engine = AnalyticsEngine(con, mapping, salt=load_or_create_salt(args.salt_file.resolve()),
                                     reveal_labels=args.reveal_labels, debug=args.debug)
            f = Filters(args.date_from, args.date_to, tuple(args.rig), tuple(args.site), tuple(args.blast))
            if f.date_from and f.date_to and f.date_from > f.date_to:
                print("ERROR: --from is after --to", file=sys.stderr)
                return 2
            if args.command == "readiness":
                r = predictions.assess_readiness(engine, f)
                print(f"Prediction readiness: {'READY' if r['ready'] else 'NOT READY'}")
                for x in r["reasons"]:
                    print(f"  - {x}")
                return 0
            report = run_analytics(engine, f, grain=args.grain, compare=args.compare_blast, train=args.train,
                                   models_dir=args.models_dir.resolve())
    except duckdb.IOException as exc:
        print(f"ERROR: cannot open database read-only ({safe_error(exc)}).", file=sys.stderr)
        return 3
    except duckdb.Error as exc:
        if args.debug:
            traceback.print_exc()
        print(f"ERROR: database error ({safe_error(exc)}).", file=sys.stderr)
        return 1

    print_summary(report)
    out_dir = args.output_dir.resolve()
    paths = [out_dir / JSON_REPORT, out_dir / MD_REPORT]
    if confirm_overwrite(paths, force=args.force):
        write_json_atomic(paths[0], report)
        write_text_atomic(paths[1], render_markdown(report))
        print("Wrote:\n  " + "\n  ".join(str(p) for p in paths))
    else:
        print("Reports not written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
