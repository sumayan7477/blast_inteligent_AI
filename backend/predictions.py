"""
Drilling-duration prediction: readiness assessment and an optional baseline model.

Readiness (always computed, counts only):
* target = drilling duration in minutes (mapped duration column, or end - start);
  needs >= 300 valid (> 0) targets with non-zero variance
* candidate features known BEFORE drilling finishes: depth, hole measures,
  rig, site, and hour/weekday/month of start_time
* excluded for leakage: end_time, duration, status, problem_flag, identifiers
* a feature is usable when <= 50 % missing on rows with a valid target

Training (only with ``--train`` AND ready):
* time-based split (earliest 80 % train, latest 20 % test) when start_time
  exists, otherwise a seeded random 80/20 split
* DummyRegressor(median) baseline vs HistGradientBoostingRegressor(loss="absolute_error")
  (robust to extreme durations; squared loss let a few outliers distort whole leaves)
* MAE, RMSE, median AE, R^2 on the held-out test set
* the fitted pipeline is saved to ``models/`` (Git-ignored). It contains
  category labels seen in training, so it must stay local.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import database as db
from report_io import write_json_atomic

if TYPE_CHECKING:  # pragma: no cover
    from analytics import AnalyticsEngine, Filters

MIN_TARGET_ROWS = 300
MAX_FEATURE_MISSING_PCT = 50.0
MAX_CATEGORY_LEVELS = 200
MAX_TRAIN_ROWS = 500_000
MIN_IMPROVEMENT_PCT = 10.0
MODEL_FILE = "drilling_duration_baseline.joblib"
LEAKAGE_ROLES = {
    "end_time": "known only after drilling finishes",
    "duration": "is the target",
    "status": "outcome recorded after drilling",
    "problem_flag": "outcome recorded during/after drilling",
    "hole_id": "identifier, not a predictive feature",
    "blast_id": "identifier, not a predictive feature",
}


def _feature_sql(engine: "AnalyticsEngine") -> dict[str, tuple[str, str]]:
    """name -> (sql, kind) for candidate features."""
    h = engine.holes
    out: dict[str, tuple[str, str]] = {}
    if h.has("depth"):
        out["depth"] = (f"CASE WHEN {h.q('depth')} > 0 THEN CAST({h.q('depth')} AS DOUBLE) END", "numeric")
    target_cols = {h.columns.get("duration"), h.columns.get("depth")}
    for m in h.measures:
        if m not in target_cols:
            out[m] = (f"CAST({db.quote_ident(m)} AS DOUBLE)", "numeric")
    for role in ("rig", "site"):
        if h.has(role):
            out[role] = (f"CAST({h.q(role)} AS VARCHAR)", "categorical")
    if h.has("start_time"):
        s = h.q("start_time")
        out["start_hour"] = (f"CAST(hour({s}) AS DOUBLE)", "numeric")
        out["start_weekday"] = (f"CAST(isodow({s}) AS DOUBLE)", "numeric")
        out["start_month"] = (f"CAST(month({s}) AS DOUBLE)", "numeric")
    return out


def assess_readiness(engine: "AnalyticsEngine", f: "Filters") -> dict[str, Any]:
    from analytics import FilterNotApplicable

    h = engine.holes
    result: dict[str, Any] = {"ready": False, "target": None, "reasons": [], "features": [], "excluded": []}
    if not h:
        result["reasons"].append("holes entity not confirmed")
        return result
    d = engine.duration_expr(h)
    if not d:
        result["reasons"].append("no reliable target: map holes.duration, or both start_time and end_time")
        return result
    expr, method, cols = d
    result["target"] = {"name": "drilling_duration_minutes", "definition": method, "columns": cols,
                        "view": h.view}
    try:
        w, p = engine.where(h, f)
        wv, pv = engine.where(h, f, [f"{expr} > 0"])
    except FilterNotApplicable as exc:
        result["reasons"].append(str(exc))
        return result
    rows, known, valid, distinct, mean, sd = engine.one(
        f"SELECT count(*), count({expr}), count(*) FILTER (WHERE {expr} > 0), "
        f"count(DISTINCT round({expr}, 2)) FILTER (WHERE {expr} > 0), avg({expr}) FILTER (WHERE {expr} > 0), "
        f"stddev_samp({expr}) FILTER (WHERE {expr} > 0) FROM {engine.view(h)}{w}", p)
    cv = (sd / mean) if mean and sd is not None else None
    result["target_quality"] = {
        "rows": int(rows), "rows_with_target": int(known), "valid_targets": int(valid),
        "invalid_targets_non_positive": int(known - valid), "missing_target": int(rows - known),
        "distinct_values": int(distinct), "coefficient_of_variation": round(cv, 3) if cv is not None else None,
    }

    feats = _feature_sql(engine)
    for name, (sql, kind) in feats.items():
        n_present, n_distinct = engine.one(
            f"SELECT count({sql}), count(DISTINCT {sql}) FROM {engine.view(h)}{wv}", pv)
        miss = round(100 * (valid - n_present) / valid, 2) if valid else None
        usable, note = True, None
        if miss is None or miss > MAX_FEATURE_MISSING_PCT:
            usable, note = False, f"more than {MAX_FEATURE_MISSING_PCT:.0f}% missing"
        elif n_distinct < 2:
            usable, note = False, "constant"
        elif kind == "categorical" and n_distinct > MAX_CATEGORY_LEVELS:
            usable, note = False, f"more than {MAX_CATEGORY_LEVELS} levels"
        result["features"].append({"feature": name, "kind": kind, "missing_pct": miss,
                                   "distinct_values": int(n_distinct), "usable": usable, "note": note})
    for role, why in LEAKAGE_ROLES.items():
        if h.has(role):
            result["excluded"].append({"role": role, "column": h.columns[role], "reason": why})

    usable = [x for x in result["features"] if x["usable"]]
    result["split_strategy"] = ("time-based: earliest 80% by start_time for training, latest 20% for testing"
                                if h.has("start_time") else "random 80/20 split (seed 42) - no start_time for a time split")
    if valid < MIN_TARGET_ROWS:
        result["reasons"].append(f"only {valid:,} valid duration targets (need >= {MIN_TARGET_ROWS})")
    if not cv:
        result["reasons"].append("target has no variance")
    if not usable:
        result["reasons"].append(f"no feature with <= {MAX_FEATURE_MISSING_PCT:.0f}% missing values")
    result["ready"] = not result["reasons"]
    if result["ready"]:
        result["reasons"].append(f"{valid:,} valid targets and {len(usable)} usable features "
                                 "- a baseline model can be trained with --train")
    return result


def _metrics(y_true: Any, y_pred: Any) -> dict[str, float]:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, median_absolute_error, r2_score
    return {"mae": round(float(mean_absolute_error(y_true, y_pred)), 3),
            "rmse": round(float(math.sqrt(mean_squared_error(y_true, y_pred))), 3),
            "median_ae": round(float(median_absolute_error(y_true, y_pred)), 3),
            "r2": round(float(r2_score(y_true, y_pred)), 4)}


def train_baseline(engine: "AnalyticsEngine", f: "Filters", readiness: dict[str, Any],
                   models_dir: Path) -> dict[str, Any]:
    if not readiness.get("ready"):
        return {"trained": False, "reason": "data not sufficient - see readiness reasons"}
    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.dummy import DummyRegressor
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.impute import SimpleImputer
        from sklearn.model_selection import train_test_split
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder
        import joblib
    except ImportError as exc:
        return {"trained": False, "reason": f"scikit-learn not available ({exc.name})"}

    h = engine.holes
    expr = engine.duration_expr(h)[0]
    feats = _feature_sql(engine)
    usable = [x["feature"] for x in readiness["features"] if x["usable"]]
    num = [n for n in usable if feats[n][1] == "numeric"]
    cat = [n for n in usable if feats[n][1] == "categorical"]
    order = f"{h.q('start_time')}" if h.has("start_time") else None
    w, p = engine.where(h, f, [f"{expr} > 0"])
    select = ", ".join([f"{expr} AS __target"] + [f"{feats[n][0]} AS {db.quote_ident(n)}" for n in usable]
                       + ([f"epoch({order}) AS __t"] if order else []))
    frame = engine.con.execute(
        f"SELECT {select} FROM {engine.view(h)}{w} USING SAMPLE reservoir({MAX_TRAIN_ROWS} ROWS) REPEATABLE (42)",
        p).df()

    if order:
        frame = frame.sort_values("__t", kind="mergesort").reset_index(drop=True)
        cut = int(len(frame) * 0.8)
        train, test = frame.iloc[:cut], frame.iloc[cut:]
        split = "time-based 80/20 by start_time"
    else:
        train, test = train_test_split(frame, test_size=0.2, random_state=42)
        split = "random 80/20 (seed 42)"
    if len(test) < 30 or len(train) < 100:
        return {"trained": False, "reason": f"split too small (train {len(train)}, test {len(test)})"}

    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), num),
        ("cat", Pipeline([("imp", SimpleImputer(strategy="constant", fill_value="__missing__")),
                          ("oh", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=engine.k,
                                               sparse_output=False))]), cat),
    ])
    # absolute_error loss: robust to the extreme durations real drilling logs contain, and aligned with MAE.
    model = Pipeline([("pre", pre), ("reg", HistGradientBoostingRegressor(loss="absolute_error", random_state=42,
                                                                          max_iter=300))])
    x_tr, y_tr, x_te, y_te = train[usable], train["__target"], test[usable], test["__target"]
    baseline = DummyRegressor(strategy="median").fit(x_tr, y_tr)
    model.fit(x_tr, y_tr)
    m_base, m_model = _metrics(y_te, baseline.predict(x_te)), _metrics(y_te, model.predict(x_te))
    improvement = round(100 * (m_base["mae"] - m_model["mae"]) / m_base["mae"], 2) if m_base["mae"] else None
    useful = improvement is not None and improvement >= MIN_IMPROVEMENT_PCT

    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / MODEL_FILE
    joblib.dump(model, model_path)
    meta = {
        "trained_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": readiness["target"], "features_numeric": num, "features_categorical": cat,
        "split": split, "train_rows": int(len(train)), "test_rows": int(len(test)),
        "metrics": {"median_baseline": m_base, "hist_gradient_boosting": m_model},
        "mae_improvement_vs_baseline_pct": improvement, "filters": f.describe(),
        "note": "Model file contains category labels from training data - keep it local.",
    }
    write_json_atomic(models_dir / (MODEL_FILE + ".meta.json"), meta)
    return {
        "trained": True, "split": split, "train_rows": int(len(train)), "test_rows": int(len(test)),
        "features_numeric": num, "features_categorical": cat,
        "metrics": {"median_baseline": m_base, "hist_gradient_boosting": m_model},
        "mae_improvement_vs_baseline_pct": improvement,
        "verdict": (f"model beats the median baseline by {improvement}% MAE - usable as a first estimate"
                    if useful else f"model does not beat the median baseline by >= {MIN_IMPROVEMENT_PCT:.0f}% "
                                   "- do not rely on it"),
        "model_path": str(model_path),
        "units": "minutes",
    }

