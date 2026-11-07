"""
Anomaly detection for drilling and blast records (statistical, explainable).

Detectors (each runs only if the mapped roles support it):

* ``hole_duration``      robust z-score of drilling duration, per rig when a rig is mapped
* ``hole_depth``         robust z-score of hole depth
* ``drilling_rate``      robust z-score of depth per hour, per rig when mapped
* ``holes_per_blast``    robust z-score of hole count per blast
* ``blast_<measure>``    robust z-score of each configured blast measure
* ``invalid_intervals``  data quality: end_time not after start_time
* ``isolation_forest``   multivariate Isolation Forest on hole features - only with
                         >= 500 rows and >= 2 features with <= 30 % missing

Robust z = (value - median) / (1.4826 * MAD), computed inside DuckDB per group.
A record is flagged when |robust z| > 3.5 and its group has >= 20 records.

Every flag carries lineage (``_record_key``, source file, line) and an
explanation in z-scores/percentiles - never the raw value - and the label
"statistical outlier ... NOT a confirmed operational problem".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import duckdb

import database as db
from logging_setup import safe_error

if TYPE_CHECKING:  # pragma: no cover
    from analytics import AnalyticsEngine, Filters

FLAG_LABEL = "Statistical outlier for human review - NOT a confirmed operational problem."
ROBUST_Z_THRESHOLD = 3.5
MIN_GROUP_FOR_SCORING = 20
MAX_FLAGGED_LISTED = 50
IF_MIN_ROWS = 500
IF_SAMPLE_ROWS = 100_000
IF_MAX_FEATURE_MISSING = 0.30
IF_CONTAMINATION = 0.01
LINEAGE = ("_record_key", "_source_file", "_source_line")


def _detector(detector: str, title: str, *, status: str = "ok", reason: str | None = None, **kw: Any) -> dict:
    return {"detector": detector, "title": title, "status": status, "reason": reason, "method": kw.pop("method", ""),
            "sources": kw.pop("sources", []), "records_scored": kw.pop("records_scored", None),
            "groups_scored": kw.pop("groups_scored", None), "flagged_count": kw.pop("flagged_count", None),
            "flagged": kw.pop("flagged", []), **kw}


def robust_outliers(engine: "AnalyticsEngine", *, detector: str, title: str, feature: str, base_sql: str,
                    params: list[Any], record_cols: tuple[str, ...], scope_name: str | None,
                    sources: list[dict]) -> dict[str, Any]:
    """Robust z-score outliers. ``base_sql`` must yield ``record_cols..., g, v``."""
    rc = ", ".join(f"b.{c}" for c in record_cols)
    ctes = f"""
        WITH base AS ({base_sql}),
        st AS (SELECT g, median(v) AS med, count(*) AS n FROM base GROUP BY g),
        md AS (SELECT b.g, median(abs(b.v - st.med)) AS mad FROM base b JOIN st USING (g) GROUP BY b.g),
        sc AS (SELECT {rc}, b.g, st.n, (b.v - st.med) / (1.4826 * md.mad) AS rz,
                      percent_rank() OVER (PARTITION BY b.g ORDER BY b.v) AS pr
               FROM base b JOIN st USING (g) JOIN md USING (g)
               WHERE st.n >= {MIN_GROUP_FOR_SCORING} AND md.mad > 0)"""
    scored, groups_ok = engine.one(f"{ctes} SELECT count(*), count(DISTINCT g) FROM sc", params)
    total, groups_all = engine.one(f"{ctes} SELECT count(*), count(DISTINCT g) FROM base", params)
    (n_flag,) = engine.one(f"{ctes} SELECT count(*) FROM sc WHERE abs(rz) > {ROBUST_Z_THRESHOLD}", params)
    rows = engine.rows(f"{ctes} SELECT * FROM sc WHERE abs(rz) > {ROBUST_Z_THRESHOLD} "
                       f"ORDER BY abs(rz) DESC LIMIT {MAX_FLAGGED_LISTED}", params)
    flagged = []
    for r in rows:
        rec = dict(zip(record_cols, r[: len(record_cols)]))
        g, n, rz, pr = r[len(record_cols):]
        scope = f"{scope_name} {g}" if scope_name else "all records"
        flagged.append({
            "record": rec if "_record_key" in rec else None,
            "group": rec.get("blast") or (g if scope_name else None),
            "robust_z": round(float(rz), 2),
            "percentile": round(100 * float(pr), 1),
            "explanation": (f"{feature} is {abs(rz):.1f} robust SDs {'above' if rz > 0 else 'below'} the median "
                            f"of {scope} (n={n}); percentile {100 * pr:.1f}"),
            "label": FLAG_LABEL,
        })
    status, reason = "ok", None
    if total == 0:
        status, reason = "unavailable", "no values to score"
    elif scored == 0:
        status, reason = "unavailable", (f"no group has >= {MIN_GROUP_FOR_SCORING} records with non-zero spread "
                                         "(cannot score robustly)")
    return _detector(detector, title, status=status, reason=reason,
                     method=(f"robust z = (x - median) / (1.4826 * MAD)"
                             + (f" within each {scope_name}" if scope_name else " over all records")
                             + f"; flag |z| > {ROBUST_Z_THRESHOLD}; groups need >= {MIN_GROUP_FOR_SCORING} records"),
                     sources=sources, records_scored=int(scored), records_considered=int(total),
                     groups_scored=int(groups_ok), groups_not_scored=int(groups_all - groups_ok),
                     flagged_count=int(n_flag), flagged=flagged)


def _lineage_base(engine: "AnalyticsEngine", ent, value_sql: str, f: "Filters", group_role: str | None,
                  prefix: str, extra: list[str]) -> tuple[str, list[Any], str | None]:
    use_group = group_role is not None and ent.has(group_role)
    g = engine.label(ent.q(group_role), prefix) if use_group else "'all'"
    w, p = engine.where(ent, f, [f"({value_sql}) IS NOT NULL"] + extra)
    base = (f"SELECT {', '.join(LINEAGE)}, {g} AS g, CAST({value_sql} AS DOUBLE) AS v "
            f"FROM {engine.view(ent)}{w}")
    return base, p, (group_role if use_group else None)


def isolation_forest(engine: "AnalyticsEngine", f: "Filters") -> dict[str, Any]:
    title = "Multivariate outliers (Isolation Forest)"
    h = engine.holes
    if not h:
        return _detector("isolation_forest", title, status="unavailable", reason="holes entity not confirmed")
    feats: dict[str, str] = {}
    d = engine.duration_expr(h)
    if d:
        feats["duration_minutes"] = f"CASE WHEN {d[0]} > 0 THEN {d[0]} END"
    if h.has("depth"):
        feats["depth"] = f"CASE WHEN {h.q('depth')} > 0 THEN {h.q('depth')} END"
    if d and h.has("depth"):
        feats["drilling_rate"] = f"CASE WHEN {d[0]} > 0 AND {h.q('depth')} > 0 THEN {h.q('depth')} / ({d[0]} / 60.0) END"
    for m in h.measures:
        feats.setdefault(m, db.quote_ident(m))
    if len(feats) < 2:
        return _detector("isolation_forest", title, status="unavailable",
                         reason="needs at least 2 numeric hole features (duration, depth or measures)")
    w, p = engine.where(h, f)
    (n,) = engine.one(f"SELECT count(*) FROM {engine.view(h)}{w}", p)
    if n < IF_MIN_ROWS:
        return _detector("isolation_forest", title, status="unavailable",
                         reason=f"needs >= {IF_MIN_ROWS} hole records (have {n})")
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError as exc:
        return _detector("isolation_forest", title, status="unavailable", reason=f"scikit-learn missing ({exc.name})")

    cols = ", ".join(f"CAST({sql} AS DOUBLE) AS {db.quote_ident(name)}" for name, sql in feats.items())
    frame = engine.con.execute(
        f"SELECT {', '.join(LINEAGE)}, {cols} FROM {engine.view(h)}{w} "
        f"USING SAMPLE reservoir({IF_SAMPLE_ROWS} ROWS) REPEATABLE (42)", p).df()
    usable = [c for c in feats if frame[c].isna().mean() <= IF_MAX_FEATURE_MISSING]
    dropped = [c for c in feats if c not in usable]
    if len(usable) < 2:
        return _detector("isolation_forest", title, status="unavailable",
                         reason=f"fewer than 2 features with <= {IF_MAX_FEATURE_MISSING:.0%} missing")
    x = frame[usable].to_numpy(dtype=float)
    med = np.nanmedian(x, axis=0)
    q75, q25 = np.nanpercentile(x, 75, axis=0), np.nanpercentile(x, 25, axis=0)
    scale = np.where((q75 - q25) > 0, q75 - q25, np.nanstd(x, axis=0))
    scale = np.where(scale > 0, scale, 1.0)
    x = np.where(np.isnan(x), med, x)
    z = (x - med) / scale
    model = IsolationForest(n_estimators=200, contamination=IF_CONTAMINATION, random_state=42)
    pred = model.fit_predict(z)
    score = -model.score_samples(z)
    idx = np.where(pred == -1)[0]
    idx = idx[np.argsort(-score[idx])][:MAX_FLAGGED_LISTED]
    rank = score.argsort().argsort() / max(len(score) - 1, 1)
    flagged = []
    for i in idx:
        top = np.argsort(-np.abs(z[i]))[:2]
        why = "; ".join(f"{usable[j]} unusually {'high' if z[i, j] > 0 else 'low'} (robust z {z[i, j]:+.1f})"
                        for j in top)
        flagged.append({
            "record": {c: (int(frame.at[i, c]) if c == "_source_line" else frame.at[i, c]) for c in LINEAGE},
            "isolation_percentile": round(100 * float(rank[i]), 2),
            "explanation": f"isolated by the model (top {100 * (1 - rank[i]):.2f}% most isolated); {why}",
            "label": FLAG_LABEL,
        })
    return _detector(
        "isolation_forest", title,
        method=(f"IsolationForest(n_estimators=200, contamination={IF_CONTAMINATION}, random_state=42) on robust-scaled "
                f"features {usable}; median imputation; fitted on a reservoir sample of up to {IF_SAMPLE_ROWS:,} rows"),
        sources=[engine.source(h, "depth", columns=(d[2] if d else []) + list(h.measures))],
        records_scored=int(len(frame)), records_considered=int(n), flagged_count=int((pred == -1).sum()),
        features_used=usable, features_dropped_missing=dropped, flagged=flagged)


def detector_requirements(engine: "AnalyticsEngine") -> list[dict[str, Any]]:
    """Which detectors the confirmed mapping supports - the same conditions ``detect_all`` uses (no queries)."""
    h, b = engine.holes, engine.blasts
    d = engine.duration_expr(h) if h else None
    rows = [
        ("hole_duration", "Unusual drilling duration", bool(h and d),
         "needs a drilling duration (duration column with a stated unit, or start and end timestamps)"),
        ("hole_depth", "Unusual hole depth", bool(h and h.has("depth")), "needs holes.depth"),
        ("drilling_rate", "Unusual drilling rate", bool(h and d and h.has("depth")), "needs depth and a duration"),
        ("holes_per_blast", "Unusual number of holes per blast", bool(h and h.has("blast_id")), "needs holes.blast_id"),
        ("blast_measures", "Unusual blast measures", bool(b and b.measures), "needs a confirmed blasts entity with measures"),
        ("invalid_intervals", "Invalid drilling intervals (data quality)", bool(h and h.has("start_time", "end_time")),
         "needs start and end timestamps"),
        ("isolation_forest", "Multivariate outliers (Isolation Forest)", bool(h),
         f"needs a confirmed holes entity; at run time also >= 2 numeric features and >= {IF_MIN_ROWS} records"),
    ]
    return [{"detector": k, "title": t, "supported": ok, "requirement": req} for k, t, ok, req in rows]


def detect_all(engine: "AnalyticsEngine", f: "Filters") -> dict[str, Any]:
    from analytics import FilterNotApplicable  # local import avoids a cycle

    detectors: list[dict[str, Any]] = []

    def run(name: str, title: str, fn) -> None:
        try:
            detectors.append(fn())
        except FilterNotApplicable as exc:
            detectors.append(_detector(name, title, status="unavailable", reason=str(exc)))
        except duckdb.Error as exc:
            detectors.append(_detector(name, title, status="error", reason=f"query failed ({safe_error(exc)})"))

    h, b = engine.holes, engine.blasts
    d = engine.duration_expr(h) if h else None
    if h and d:
        def dur() -> dict:
            base, p, scope = _lineage_base(engine, h, d[0], f, "rig", "RIG", [f"{d[0]} > 0"])
            return robust_outliers(engine, detector="hole_duration", title="Unusual drilling duration",
                                   feature="drilling duration", base_sql=base, params=p, record_cols=LINEAGE,
                                   scope_name=scope, sources=[engine.source(h, "rig", columns=d[2])])
        run("hole_duration", "Unusual drilling duration", dur)
    if h and h.has("depth"):
        def depth() -> dict:
            base, p, scope = _lineage_base(engine, h, h.q("depth"), f, None, "", [f"{h.q('depth')} > 0"])
            return robust_outliers(engine, detector="hole_depth", title="Unusual hole depth", feature="hole depth",
                                   base_sql=base, params=p, record_cols=LINEAGE, scope_name=scope,
                                   sources=[engine.source(h, "depth")])
        run("hole_depth", "Unusual hole depth", depth)
    if h and d and h.has("depth"):
        def rate() -> dict:
            expr = f"{h.q('depth')} / ({d[0]} / 60.0)"
            base, p, scope = _lineage_base(engine, h, expr, f, "rig", "RIG", [f"{d[0]} > 0", f"{h.q('depth')} > 0"])
            return robust_outliers(engine, detector="drilling_rate", title="Unusual drilling rate",
                                   feature="drilling rate (depth per hour)", base_sql=base, params=p,
                                   record_cols=LINEAGE, scope_name=scope,
                                   sources=[engine.source(h, "depth", "rig", columns=d[2])])
        run("drilling_rate", "Unusual drilling rate", rate)
    if h and h.has("blast_id"):
        def per_blast() -> dict:
            w, p = engine.where(h, f, [f"{h.q('blast_id')} IS NOT NULL"])
            base = (f"SELECT {engine.label(h.q('blast_id'), 'BLAST')} AS blast, 'all' AS g, CAST(count(*) AS DOUBLE) AS v "
                    f"FROM {engine.view(h)}{w} GROUP BY blast")
            return robust_outliers(engine, detector="holes_per_blast", title="Unusual number of holes per blast",
                                   feature="holes per blast", base_sql=base, params=p, record_cols=("blast",),
                                   scope_name=None, sources=[engine.source(h, "blast_id")])
        run("holes_per_blast", "Unusual number of holes per blast", per_blast)
    if b:
        for m in b.measures:
            def measure(m: str = m) -> dict:
                q = db.quote_ident(m)
                base, p, scope = _lineage_base(engine, b, q, f, None, "", [])
                return robust_outliers(engine, detector=f"blast_{m}", title=f"Unusual blast {m}", feature=m,
                                       base_sql=base, params=p, record_cols=LINEAGE, scope_name=scope,
                                       sources=[engine.source(b, columns=[m])])
            run(f"blast_{m}", f"Unusual blast {m}", measure)
    if h and h.has("start_time", "end_time"):
        def invalid() -> dict:
            s, e = h.q("start_time"), h.q("end_time")
            w, p = engine.where(h, f, [f"{s} IS NOT NULL", f"{e} IS NOT NULL", f"{e} <= {s}"])
            (n,) = engine.one(f"SELECT count(*) FROM {engine.view(h)}{w}", p)
            rows = engine.rows(f"SELECT {', '.join(LINEAGE)} FROM {engine.view(h)}{w} "
                               f"ORDER BY _source_file, _source_line LIMIT {MAX_FLAGGED_LISTED}", p)
            return _detector("invalid_intervals", "Invalid drilling intervals (data quality)",
                             method="end_time <= start_time", sources=[engine.source(h, "start_time", "end_time")],
                             records_scored=None, flagged_count=int(n),
                             flagged=[{"record": dict(zip(LINEAGE, r)),
                                       "explanation": "end_time is not after start_time (data-quality issue)",
                                       "label": FLAG_LABEL} for r in rows])
        run("invalid_intervals", "Invalid drilling intervals (data quality)", invalid)
    run("isolation_forest", "Multivariate outliers (Isolation Forest)", lambda: isolation_forest(engine, f))
    if not detectors:
        detectors.append(_detector("none", "Anomaly detection", status="unavailable",
                                   reason="no mapped numeric roles support anomaly detection"))
    return {"label": FLAG_LABEL, "robust_z_threshold": ROBUST_Z_THRESHOLD, "detectors": detectors}
