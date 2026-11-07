"""
Semantic mapping between analytic concepts and the approved typed views.

The analytics engine never assumes field names. Instead it uses roles
(``blast_id``, ``start_time``, ``depth`` ...) that are mapped to real view
columns in ``local_reports/analytics_mapping.json`` (Git-ignored):

    python backend/analytics.py suggest-mapping   # writes a suggestion (all unconfirmed)
    # edit the file, check each entity, set "confirmed": true
    python backend/analytics.py run

Suggestions come from generic English vocabulary matched against column
names and DuckDB column types of the views registered by typed_views.py.
Only entities with ``"confirmed": true`` are used, and every mapped column is
re-validated against the database (exists, compatible type) on each run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import duckdb

import database as db

MAPPING_VERSION = 1
DURATION_UNITS = {"seconds": 1 / 60, "minutes": 1.0, "hours": 60.0}

# role -> kind. Kinds are checked against DuckDB column types.
ENTITY_ROLES: dict[str, dict[str, str]] = {
    "blasts": {"blast_id": "id", "time": "time", "site": "label", "rig": "label"},
    "holes": {
        "hole_id": "id", "blast_id": "id", "rig": "label", "site": "label",
        "start_time": "timestamp", "end_time": "timestamp", "duration": "num", "depth": "num",
        "status": "status", "problem_flag": "bool",
    },
    "problems": {
        "problem_id": "id", "time": "time", "rig": "label", "site": "label",
        "hole_id": "id", "blast_id": "id", "category": "label",
    },
}
ENTITY_DESCRIPTIONS = {
    "blasts": "One row per blast (or per blast record).",
    "holes": "One row per drilled hole / drilling record.",
    "problems": "One row per recorded drilling problem, event or comment.",
}

# Generic vocabulary only - no dataset-specific names.
ID_WORDS = {"id", "no", "num", "number", "code", "key", "name", "ref", "uuid", "guid"}
ROLE_HINTS: dict[str, tuple[tuple[str, ...], bool]] = {
    "blast_id": (("blast", "shot"), True),
    "hole_id": (("hole",), True),
    "problem_id": (("event", "problem", "issue", "comment", "incident"), True),
    "rig": (("rig", "machine", "drill", "equipment"), False),
    "site": (("site", "field", "pit", "bench", "area", "mine", "location", "project"), False),
    "time": (("date", "time", "timestamp", "at", "created", "fired", "occurred", "logged"), False),
    "start_time": (("start", "begin", "started", "from"), False),
    "end_time": (("end", "finish", "finished", "completed", "complete", "stop", "stopped"), False),
    "duration": (("duration", "elapsed", "minutes", "seconds", "hours", "time"), False),
    "depth": (("depth", "length", "meters", "metres", "meter", "metre"), False),
    "status": (("status", "state", "completed", "complete", "done", "result"), False),
    "problem_flag": (("problem", "issue", "fault", "error", "stuck", "fail", "failed", "failure"), False),
    "category": (("category", "type", "kind", "class", "reason", "code", "problem", "issue"), False),
}
VIEW_HINTS: dict[str, tuple[str, ...]] = {
    "blasts": ("blast", "blasts", "shot", "shots"),
    "holes": ("hole", "holes", "drill", "drilling"),
    "problems": ("event", "events", "problem", "problems", "issue", "issues", "comment", "comments",
                 "incident", "log"),
}

_NUMERIC = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
            "UBIGINT", "FLOAT", "REAL", "DOUBLE"}


def name_tokens(name: str) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        tokens += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
    return [t.lower() for t in tokens if t]


def kind_ok(kind: str, dtype: str) -> bool:
    t = dtype.upper()
    is_time = t == "DATE" or t.startswith("TIMESTAMP")
    is_num = t in _NUMERIC or t.startswith("DECIMAL")
    scalar = not (t == "JSON" or t.startswith(("STRUCT", "MAP", "LIST", "UNION")) or t.endswith("]"))
    return {
        "id": scalar and not is_time, "label": scalar and not is_time, "status": scalar and not is_time,
        "time": is_time, "timestamp": t.startswith("TIMESTAMP"),
        "num": is_num, "bool": t == "BOOLEAN",
    }.get(kind, False)


# ---------------------------------------------------------------------------
# Database discovery (names and types only)
# ---------------------------------------------------------------------------
def approved_views(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, str]]:
    """Views registered by typed_views.py -> {view: {column: data_type}}."""
    if not db.table_exists(con, "meta_typed_views"):
        return {}
    registered = {r[0] for r in con.execute("SELECT DISTINCT view_name FROM meta_typed_views").fetchall()}
    existing = set(db.list_tables(con, table_type="VIEW"))
    out: dict[str, dict[str, str]] = {}
    for view in sorted(registered & existing):
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [view]
        ).fetchall()
        out[view] = {c: t for c, t in cols}
    return out


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------
def _role_score(role: str, column: str, dtype: str) -> float:
    if column.startswith("_"):
        return 0.0
    kind = next(k for roles in ENTITY_ROLES.values() for r, k in roles.items() if r == role)
    if not kind_ok(kind, dtype):
        return 0.0
    words, needs_id = ROLE_HINTS[role]
    tokens = name_tokens(column)
    hit = [w for w in words if w in tokens]
    if not hit:
        return 0.0
    score = 0.5 + 0.1 * len(hit)
    if needs_id:
        if not (set(tokens) & ID_WORDS):
            return 0.0
        score += 0.2
    if role in ("time", "start_time", "end_time") and dtype.upper().startswith("TIMESTAMP"):
        score += 0.1
    return round(min(score, 1.0), 2)


def _view_score(entity: str, view: str) -> float:
    tokens = set(name_tokens(view.removeprefix("tv_").removeprefix("tj_")))
    own = len(tokens & set(VIEW_HINTS[entity]))
    others = sum(len(tokens & set(h)) for e, h in VIEW_HINTS.items() if e != entity)
    if view.startswith("tj_"):
        own -= 0.5  # prefer plain dataset views; join views are alternatives
    return own - 0.6 * others if own > 0 else 0.0


def suggest_mapping(views: dict[str, dict[str, str]]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    for entity, roles in ENTITY_ROLES.items():
        ranked = sorted(((v, _view_score(entity, v)) for v in views), key=lambda x: -x[1])
        ranked = [(v, s) for v, s in ranked if s > 0]
        view = ranked[0][0] if ranked else None
        cols: dict[str, str | None] = {}
        alternatives: dict[str, list[str]] = {"views": [v for v, _ in ranked[1:4]]}
        used: set[str] = set()
        if view:
            for role in roles:
                cand = sorted(((c, _role_score(role, c, t)) for c, t in views[view].items()), key=lambda x: -x[1])
                cand = [(c, s) for c, s in cand if s > 0]
                pick = next((c for c, _ in cand if c not in used), None)
                cols[role] = pick
                if pick:
                    used.add(pick)
                alternatives[role] = [c for c, _ in cand if c != pick][:3]
        else:
            cols = dict.fromkeys(roles)
        entry: dict[str, Any] = {
            "description": ENTITY_DESCRIPTIONS[entity],
            "confirmed": False,
            "view": view,
            "columns": cols,
            "alternatives": alternatives,
        }
        if entity in ("blasts", "holes") and view:
            entry["measures"] = [c for c, t in views[view].items()
                                 if not c.startswith("_") and kind_ok("num", t) and c not in used]
        elif entity in ("blasts", "holes"):
            entry["measures"] = []
        if entity == "holes":
            entry["duration_unit"] = "minutes"
            entry["completed_values"] = []
        entities[entity] = entry
    return {
        "_instructions": [
            "SUGGESTIONS ONLY - nothing is used until an entity has \"confirmed\": true.",
            "For each entity check 'view' and every column role; set a role to null if it does not apply.",
            "holes.duration_unit: seconds | minutes | hours (only used if a 'duration' column is mapped).",
            "holes.completed_values: status values that mean 'completed' (not needed if status is BOOLEAN).",
            "measures: numeric columns used for blast comparisons and anomaly detection.",
            "privacy.min_group_size: groups smaller than this are suppressed in all outputs.",
        ],
        "mapping_version": MAPPING_VERSION,
        "privacy": {"min_group_size": 5, "pseudonymize_labels": True},
        "entities": entities,
        "available_views": {v: sorted(c for c in cols if not c.startswith("_")) for v, cols in views.items()},
    }


# ---------------------------------------------------------------------------
# Validation / resolution
# ---------------------------------------------------------------------------
@dataclass
class EntityMap:
    name: str
    view: str
    columns: dict[str, str]                       # role -> column
    types: dict[str, str]                         # column -> DuckDB type
    measures: list[str] = field(default_factory=list)
    duration_unit: str = "minutes"
    completed_values: list[str] = field(default_factory=list)

    def has(self, *roles: str) -> bool:
        return all(r in self.columns for r in roles)

    def q(self, role: str) -> str:
        return db.quote_ident(self.columns[role])

    def type_of(self, role: str) -> str:
        return self.types[self.columns[role]].upper()


@dataclass
class ResolvedMapping:
    entities: dict[str, EntityMap]
    issues: list[str]
    min_group_size: int = 5
    pseudonymize: bool = True

    def get(self, entity: str) -> EntityMap | None:
        return self.entities.get(entity)


def resolve_mapping(raw: Any, views: dict[str, dict[str, str]]) -> ResolvedMapping:
    issues: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("entities"), dict):
        raise ValueError("analytics mapping must be an object with an 'entities' object")
    privacy = raw.get("privacy") if isinstance(raw.get("privacy"), dict) else {}
    k = privacy.get("min_group_size", 5)
    if not isinstance(k, int) or k < 1:
        issues.append("privacy.min_group_size must be a positive integer - using 5")
        k = 5
    pseudo = privacy.get("pseudonymize_labels", True) is not False
    if not views:
        issues.append("No approved typed views found (run typed_views.py first).")

    resolved: dict[str, EntityMap] = {}
    for name, roles in ENTITY_ROLES.items():
        entry = raw["entities"].get(name)
        if not isinstance(entry, dict) or entry.get("confirmed") is not True:
            issues.append(f"{name}: not confirmed - analytics using it are unavailable.")
            continue
        view = entry.get("view")
        if view not in views:
            issues.append(f"{name}: view {view!r} is not an approved typed view - entity ignored.")
            continue
        types = views[view]
        cols: dict[str, str] = {}
        for role, col in (entry.get("columns") or {}).items():
            if col is None:
                continue
            if role not in roles:
                issues.append(f"{name}: unknown role {role!r} ignored.")
            elif col not in types:
                issues.append(f"{name}.{role}: column {col!r} not found in {view} - role ignored.")
            elif not kind_ok(roles[role], types[col]):
                issues.append(f"{name}.{role}: column {col!r} has type {types[col]}, expected {roles[role]} - role ignored.")
            else:
                cols[role] = col
        measures = []
        for m in entry.get("measures", []) or []:
            if m in types and kind_ok("num", types[m]):
                measures.append(m)
            else:
                issues.append(f"{name}: measure {m!r} missing or not numeric - ignored.")
        unit = entry.get("duration_unit", "minutes")
        if unit not in DURATION_UNITS:
            issues.append(f"{name}: duration_unit {unit!r} invalid - using minutes.")
            unit = "minutes"
        completed = entry.get("completed_values", []) or []
        if not isinstance(completed, list) or not all(isinstance(v, str) for v in completed):
            issues.append(f"{name}: completed_values must be a list of strings - ignored.")
            completed = []
        resolved[name] = EntityMap(name, view, cols, types, measures, unit, completed)
    return ResolvedMapping(resolved, issues, k, pseudo)
