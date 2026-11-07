"""
Local schema discovery and relationship-candidate analysis.

Input : ``local_reports/data_profile.json`` (written by ``data_inspector.py``).
        Falls back to the per-file ``local_reports/profile_*.json`` reports.
        The original JSONL files are NEVER opened.
Output: ``local_reports/schema_summary.json``
        ``local_reports/relationship_candidates.json``
        ``local_reports/schema_mapping_template.json``   (you edit this one)
        plus an interactive terminal report.

Evidence model
--------------
The profile contains field names, JSON types, missing counts, distinct-value
counts (capped) and string-length ranges - never values. Relationships are
therefore inferred only from:

* naming   - e.g. a field whose name is "<other dataset entity> + id", or an
             identical non-generic identifier name in two datasets
* types    - the two fields must hold compatible JSON types
* lengths  - string-length ranges must overlap
* cardinality - target must be a primary-key candidate; source distinct count
             should not exceed target distinct count

Value overlap between files is NOT measured, so every relationship is a
``candidate`` until you confirm it in ``schema_mapping_template.json``.
Rejected name matches are listed too, with the reason, for transparency.

Usage
-----
    python backend/schema_analyzer.py                  # analyse, write, interactive menu
    python backend/schema_analyzer.py --no-interactive # print summary only
    python backend/schema_analyzer.py --no-write       # report without writing files
    python backend/schema_analyzer.py --force          # overwrite reports without asking
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from report_io import (  # noqa: E402
    ReportError,
    confirm_overwrite,
    is_interactive,
    load_json_report,
    write_json_atomic,
)

ANALYZER_VERSION = "1.0"
TEMPLATE_VERSION = 1

PROFILE_PATH = config.REPORTS_DIR / "data_profile.json"
SUMMARY_NAME = "schema_summary.json"
RELATIONSHIPS_NAME = "relationship_candidates.json"
TEMPLATE_NAME = "schema_mapping_template.json"

# Name heuristics (generic English identifier vocabulary - no dataset field names).
ID_TOKENS = ("identifier", "number", "uuid", "guid", "code", "key", "num", "ref", "id", "no")
GENERIC_ID_NAMES = {"id", "uuid", "guid", "key", "identifier"}
DATE_NAME_HINTS = ("date", "time", "timestamp", "created", "updated", "modified", "start", "end", "at")

HIGH_MISSING_PCT = 50.0
MIXED_TYPE_MIN_SHARE = 0.01


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------
def name_tokens(name: str) -> list[str]:
    """Split ``holeId`` / ``hole_id`` / ``HOLE-ID`` into ``['hole', 'id']``."""
    tokens: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        tokens += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
    return [t.lower() for t in tokens if t]


def leaf_name(path: str) -> str:
    return path.split(".")[-1].replace("[]", "") or path


def entity_name(dataset: str) -> str:
    stem = Path(dataset).stem
    return re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper() or "DATASET"


def suggested_table(dataset: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(dataset).stem).strip("_").lower()
    if not stem or stem[0].isdigit():
        stem = f"f_{stem}"
    return f"raw_{stem}"[:63]


def split_reference_name(tokens: list[str]) -> tuple[str, str] | None:
    """``['hole','id']`` -> ``('hole','id')``; ``['holeid']`` -> ``('hole','id')``."""
    if len(tokens) > 1 and tokens[-1] in ID_TOKENS:
        return "".join(tokens[:-1]), tokens[-1]
    if len(tokens) == 1:
        word = tokens[0]
        for t in ID_TOKENS:  # longest first
            if word.endswith(t) and len(word) > len(t) + 1:
                return word[: -len(t)], t
    return None


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


# ---------------------------------------------------------------------------
# Profile model
# ---------------------------------------------------------------------------
def _int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


@dataclass
class FieldInfo:
    dataset: str
    path: str
    depth: int
    role: str
    types: dict[str, int]
    occurrences: int
    records_present: int
    records_missing: int
    missing_pct: float | None
    null_count: int
    empty_count: int
    distinct: int | None
    distinct_capped: bool
    str_len: dict[str, Any] | None
    numeric_string_count: int
    date_string_count: int
    epoch_s: int
    epoch_ms: int
    leaf: str = ""
    tokens: list[str] = field(default_factory=list)
    norm: str = ""

    def __post_init__(self) -> None:
        self.leaf = leaf_name(self.path)
        self.tokens = name_tokens(self.leaf)
        self.norm = "".join(self.tokens)

    @property
    def in_array(self) -> bool:
        return "[]" in self.path

    @property
    def dominant(self) -> str | None:
        non_null = {t: c for t, c in self.types.items() if t != "null"}
        return max(non_null, key=non_null.get) if non_null else None

    @property
    def non_null_occurrences(self) -> int:
        return max(self.occurrences - self.null_count - self.empty_count, 0)

    @property
    def distinct_label(self) -> int | str | None:
        return f">{config.INSPECTOR_DISTINCT_CAP}" if self.distinct_capped else self.distinct

    @property
    def unique_within_file(self) -> bool | None:
        """True/False if knowable from the profile, None if the distinct count was capped."""
        if self.distinct_capped:
            return None
        if self.distinct is None or self.non_null_occurrences == 0:
            return False
        return self.distinct >= self.non_null_occurrences

    @property
    def string_count(self) -> int:
        return max(self.types.get("string", 0) - self.empty_count, 0)

    @property
    def numeric_count(self) -> int:
        return self.types.get("integer", 0) + self.types.get("float", 0)


@dataclass
class Dataset:
    name: str
    valid_records: int
    complete_scan: bool
    invalid_lines: int
    limits: dict[str, Any]
    fields: list[FieldInfo]
    tokens: list[str] = field(default_factory=list)
    distinctive: list[str] = field(default_factory=list)

    @property
    def entity(self) -> str:
        return entity_name(self.name)

    def field(self, path: str) -> FieldInfo | None:
        return next((f for f in self.fields if f.path == path), None)


def _parse_distinct(value: Any) -> tuple[int | None, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return value, False
    if isinstance(value, str) and value.startswith(">"):
        return None, True
    return None, False


def _parse_field(dataset: str, raw: Any) -> FieldInfo | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
        return None
    types = raw.get("types") if isinstance(raw.get("types"), dict) else {}
    distinct, capped = _parse_distinct(raw.get("distinct_scalar_values"))
    missing_pct = raw.get("missing_pct")
    return FieldInfo(
        dataset=dataset,
        path=raw["path"],
        depth=_int(raw.get("depth")),
        role=str(raw.get("inferred_role") or "unknown"),
        types={str(k): _int(v) for k, v in types.items()},
        occurrences=_int(raw.get("occurrences")),
        records_present=_int(raw.get("records_present")),
        records_missing=_int(raw.get("records_missing_or_empty")),
        missing_pct=float(missing_pct) if isinstance(missing_pct, (int, float)) else None,
        null_count=_int(raw.get("null_count")),
        empty_count=_int(raw.get("empty_string_count")),
        distinct=distinct,
        distinct_capped=capped,
        str_len=raw.get("string_length") if isinstance(raw.get("string_length"), dict) else None,
        numeric_string_count=_int(raw.get("numeric_string_count")),
        date_string_count=_int(raw.get("date_string_count")),
        epoch_s=_int(raw.get("epoch_seconds_like_count")),
        epoch_ms=_int(raw.get("epoch_millis_like_count")),
    )


def load_profile(profile_path: Path, reports_dir: Path) -> tuple[dict[str, Any], list[Dataset], list[str]]:
    """Load and validate the profile. Returns (meta, datasets, issues)."""
    issues: list[str] = []
    if profile_path.exists():
        profile = load_json_report(profile_path, what="Profile report")
        source = str(profile_path)
    else:
        per_file = sorted(reports_dir.glob("profile_*.json"))
        if not per_file:
            raise ReportError(
                f"No profile found at {profile_path} (and no profile_*.json in {reports_dir}).\n"
                "Run the inspector first:  python backend\\data_inspector.py"
            )
        issues.append("data_profile.json missing - combined the per-file profile_*.json reports instead.")
        profile = {"files": [load_json_report(p, what="Per-file profile") for p in per_file]}
        source = f"{reports_dir}\\profile_*.json"

    if not isinstance(profile, dict) or not isinstance(profile.get("files"), list):
        raise ReportError("Profile report has an unexpected structure (expected an object with a 'files' list).")

    meta = {
        "source_profile": source,
        "report_version": profile.get("report_version"),
        "profile_generated_at_utc": profile.get("generated_at_utc"),
        "max_records_per_file": profile.get("max_records_per_file"),
        "files_failed": profile.get("files_failed") or [],
    }
    if meta["max_records_per_file"]:
        issues.append(f"Profile was built from a sample (max {meta['max_records_per_file']} records per file). "
                      "Uniqueness and missing counts may not hold for the full data - re-run the full inspector.")
    for failed in meta["files_failed"]:
        if isinstance(failed, dict):
            issues.append(f"{failed.get('file_name')}: profiling failed ({failed.get('error_type')}) - not analysed.")

    datasets: list[Dataset] = []
    seen: set[str] = set()
    for i, raw in enumerate(profile["files"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("file_name"), str):
            issues.append(f"Profile entry #{i} is malformed (no file_name) - skipped.")
            continue
        name = raw["file_name"]
        if name in seen:
            issues.append(f"{name}: appears twice in the profile - second entry skipped.")
            continue
        seen.add(name)
        raw_fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
        fields = [f for f in (_parse_field(name, r) for r in raw_fields) if f is not None]
        if len(fields) != len(raw_fields):
            issues.append(f"{name}: {len(raw_fields) - len(fields)} malformed field entries skipped.")
        lc = raw.get("line_counts") if isinstance(raw.get("line_counts"), dict) else {}
        ds = Dataset(
            name=name,
            valid_records=_int(lc.get("valid_object_records")),
            complete_scan=bool(raw.get("complete_scan", False)),
            invalid_lines=_int(lc.get("invalid_json")) + _int(lc.get("invalid_encoding")),
            limits=raw.get("limits") if isinstance(raw.get("limits"), dict) else {},
            fields=fields,
        )
        datasets.append(ds)

    if not datasets:
        raise ReportError("Profile contains no usable datasets.")

    # Distinctive name tokens per dataset (tokens shared by most datasets, e.g. a
    # common product prefix, are not useful for matching).
    for ds in datasets:
        ds.tokens = name_tokens(Path(ds.name).stem)
    token_freq = Counter(t for ds in datasets for t in set(ds.tokens))
    limit = max(len(datasets) / 2, 1)
    for ds in datasets:
        ds.distinctive = [t for t in ds.tokens if token_freq[t] <= limit] or ds.tokens
    return meta, datasets, issues


# ---------------------------------------------------------------------------
# Per-dataset analysis
# ---------------------------------------------------------------------------
def suggest_sql_type(f: FieldInfo) -> str:
    role, dom = f.role, f.dominant
    if role == "date_like_epoch":
        return "EPOCH_MS" if f.epoch_ms > f.epoch_s else "EPOCH_S"
    if role == "date_like_string":
        return "TIMESTAMP"
    if role in ("nested_object", "array"):
        return "JSON"
    if role == "boolean":
        return "BOOLEAN"
    if role == "numeric_string":
        return "DOUBLE"
    if dom == "integer":
        return "BIGINT"
    if dom == "float":
        return "DOUBLE"
    return "VARCHAR"


def identifier_evidence(f: FieldInfo) -> tuple[float, list[str]]:
    """Score how identifier-like a field is (0 = not a candidate)."""
    if f.role in ("date_like_string", "date_like_epoch", "free_text", "boolean", "nested_object",
                  "array", "always_null") or f.dominant not in ("string", "integer"):
        return 0.0, []
    score, reasons = 0.0, []
    if f.norm in GENERIC_ID_NAMES:
        score += 0.45
        reasons.append("generic identifier name")
    elif f.tokens and f.tokens[-1] in ID_TOKENS:
        score += 0.35
        reasons.append("name ends with an identifier word")
    elif split_reference_name(f.tokens):
        score += 0.2
        reasons.append("name looks like '<entity>id'")
    if f.role == "identifier_like":
        score += 0.3
        reasons.append("profiled as identifier-like (high-cardinality short strings)")
    unique = f.unique_within_file
    if unique is True:
        score += 0.15
        reasons.append("all non-null values distinct within file")
    elif unique is None:
        score += 0.05
        reasons.append(f"high cardinality (>{config.INSPECTOR_DISTINCT_CAP} distinct)")
    if f.str_len and f.str_len.get("min") == f.str_len.get("max") and _int(f.str_len.get("max")) >= 6:
        score += 0.1
        reasons.append("fixed-length strings")
    if score < 0.3:
        return 0.0, []
    return round(min(score, 1.0), 2), reasons


def primary_key_status(f: FieldInfo, ds: Dataset) -> str | None:
    """Return a PK-candidate status label, or None if the field cannot be a single-field key."""
    if f.in_array or f.records_missing > 0 or ds.valid_records == 0:
        return None
    if f.records_present != ds.valid_records:
        return None
    if f.distinct_capped:
        return "unverified_high_cardinality"
    if f.distinct == ds.valid_records:
        return "unique_in_profile" if ds.complete_scan else "unique_in_sample"
    return None


def date_evidence(f: FieldInfo) -> dict[str, Any] | None:
    hint = any(h == t or (len(h) > 2 and h in t) for t in f.tokens for h in DATE_NAME_HINTS)
    str_ratio = f.date_string_count / f.string_count if f.string_count else 0.0
    epoch_ratio = (f.epoch_s + f.epoch_ms) / f.numeric_count if f.numeric_count else 0.0
    kind = None
    if f.role == "date_like_string" or str_ratio >= 0.5:
        kind, ratio = "date_string", str_ratio
    elif f.role == "date_like_epoch" or (epoch_ratio >= 0.9 and hint):
        kind = "epoch_milliseconds" if f.epoch_ms > f.epoch_s else "epoch_seconds"
        ratio = epoch_ratio
    if kind is None:
        return None
    return {
        "field": f.path,
        "kind": kind,
        "matching_share": round(ratio, 3),
        "name_hint": hint,
        "confidence": "high" if ratio >= 0.9 and (hint or kind == "date_string") else "medium" if ratio >= 0.5 else "low",
        "suggested_sql_type": suggest_sql_type(f) if f.role.startswith("date_like") else (
            "TIMESTAMP" if kind == "date_string" else ("EPOCH_MS" if kind == "epoch_milliseconds" else "EPOCH_S")),
        "missing_pct": f.missing_pct,
        "in_array": f.in_array,
    }


def analyse_dataset(ds: Dataset) -> dict[str, Any]:
    ids, pks, dates, numeric, mixed, high_missing, ambiguous = [], [], [], [], [], [], []
    for f in ds.fields:
        score, reasons = identifier_evidence(f)
        if score:
            ids.append({"field": f.path, "score": score, "evidence": reasons, "dominant_type": f.dominant,
                        "distinct_values": f.distinct_label, "missing_pct": f.missing_pct,
                        "unique_within_file": f.unique_within_file, "in_array": f.in_array})
            status = primary_key_status(f, ds)
            if status:
                pks.append({"field": f.path, "status": status, "score": score})
        d = date_evidence(f)
        if d:
            dates.append(d)
        elif f.dominant in ("integer", "float") and any(h in f.tokens for h in ("time", "date", "timestamp")):
            ambiguous.append(f"{f.path}: name suggests a date/time but values are not epoch-like "
                             "(possibly a duration or a coded value).")
        if f.role in ("numeric", "numeric_string", "binary_flag_numeric"):
            numeric.append({"field": f.path, "role": f.role, "dominant_type": f.dominant})
        non_null = {t: c for t, c in f.types.items() if t != "null"}
        total = sum(non_null.values())
        if len(non_null) > 1 and total and sorted(non_null.values())[-2] / total >= MIXED_TYPE_MIN_SHARE:
            if set(non_null) != {"integer", "float"}:  # int/float mix is normal for numbers
                mixed.append({"field": f.path, "types": non_null})
        if f.depth == 0 and f.missing_pct is not None and f.missing_pct >= HIGH_MISSING_PCT:
            high_missing.append({"field": f.path, "missing_pct": f.missing_pct})

    ids.sort(key=lambda x: (-x["score"], x["field"]))
    pks.sort(key=lambda x: (x["status"] != "unique_in_profile", -x["score"], x["field"].count("."), x["field"]))
    roles = Counter(f.role for f in ds.fields)
    return {
        "dataset": ds.name,
        "entity": ds.entity,
        "suggested_table": suggested_table(ds.name),
        "valid_records": ds.valid_records,
        "complete_scan": ds.complete_scan,
        "invalid_lines": ds.invalid_lines,
        "field_count": len(ds.fields),
        "top_level_field_count": sum(1 for f in ds.fields if f.depth == 0),
        "nested_field_count": sum(1 for f in ds.fields if f.depth > 0),
        "array_fields": [f.path for f in ds.fields if f.role == "array" or f.dominant == "array"],
        "object_fields": [f.path for f in ds.fields if f.dominant == "object"],
        "max_depth": max((f.depth for f in ds.fields), default=0),
        "role_counts": dict(roles.most_common()),
        "identifier_candidates": ids,
        "primary_key_candidates": pks,
        "date_candidates": dates,
        "numeric_fields": numeric,
        "mixed_type_fields": mixed,
        "high_missing_fields": high_missing,
        "ambiguities": ambiguous,
        "fields": [
            {"path": f.path, "depth": f.depth, "role": f.role, "dominant_type": f.dominant, "types": f.types,
             "in_array": f.in_array, "missing_pct": f.missing_pct, "distinct_values": f.distinct_label,
             "string_length": f.str_len, "suggested_sql_type": suggest_sql_type(f)}
            for f in ds.fields
        ],
    }


# ---------------------------------------------------------------------------
# Cross-dataset relationship candidates
# ---------------------------------------------------------------------------
def _entity_match(prefix: str, target: Dataset) -> tuple[float, str] | None:
    """How well a reference-name prefix (e.g. 'hole') names the target dataset."""
    variants = {prefix, _singular(prefix)}
    whole = {"".join(target.distinctive), "".join(target.tokens)}
    if variants & whole:
        return 0.45, "field name = target dataset name + identifier word"
    if target.distinctive and variants & {target.distinctive[-1], _singular(target.distinctive[-1])}:
        return 0.4, "field name = target dataset's main entity word + identifier word"
    if variants & set(target.distinctive):
        return 0.15, "field name contains a secondary word of the target dataset name"
    return None


def _types_compatible(a: FieldInfo, b: FieldInfo) -> tuple[bool, str | None]:
    da, db_ = a.dominant, b.dominant
    if da == db_:
        return True, None
    pair = {da, db_}
    if pair == {"string", "integer"}:
        s = a if da == "string" else b
        if s.string_count and s.numeric_string_count / s.string_count >= 0.9:
            return True, "type differs (numeric strings vs integers) - needs a cast to join"
    return False, f"incompatible types ({da} vs {db_})"


def _lengths_compatible(a: FieldInfo, b: FieldInfo) -> tuple[bool, float, str | None]:
    if not (a.str_len and b.str_len) or a.dominant != "string" or b.dominant != "string":
        return True, 0.0, None
    amin, amax = _int(a.str_len.get("min")), _int(a.str_len.get("max"))
    bmin, bmax = _int(b.str_len.get("min")), _int(b.str_len.get("max"))
    if amax < bmin or bmax < amin:
        return False, 0.0, "string length ranges do not overlap"
    if amin == amax == bmin == bmax:
        return True, 0.15, "identical fixed string length"
    return True, 0.05, "string length ranges overlap"


def find_relationships(datasets: list[Dataset], analyses: dict[str, dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    by_name = {d.name: d for d in datasets}
    id_fields = {d.name: {c["field"] for c in analyses[d.name]["identifier_candidates"]} for d in datasets}
    pk_fields = {d.name: {c["field"]: c["status"] for c in analyses[d.name]["primary_key_candidates"]} for d in datasets}

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_pairs: set[frozenset] = set()

    for src in datasets:
        for a in (src.field(p) for p in sorted(id_fields[src.name])):
            if a is None:
                continue
            ref = split_reference_name(a.tokens)
            for tgt in datasets:
                if tgt.name == src.name:
                    continue
                for b_path, b_status in pk_fields[tgt.name].items():
                    b = tgt.field(b_path)
                    if b is None:
                        continue
                    evidence: list[str] = []
                    counter: list[str] = []
                    score = 0.0

                    name_ev = _entity_match(ref[0], tgt) if ref else None
                    if name_ev:
                        score += name_ev[0]
                        evidence.append(name_ev[1])
                        if ref and b.tokens and ref[1] == b.tokens[-1]:
                            score += 0.1
                            evidence.append("same identifier word as the target key")
                    elif a.norm == b.norm and a.norm not in GENERIC_ID_NAMES:
                        score += 0.35
                        evidence.append("identical identifier field name in both datasets")
                    else:
                        continue  # no naming evidence -> not even a candidate

                    base = {"from": {"dataset": src.name, "field": a.path},
                            "to": {"dataset": tgt.name, "field": b.path}}
                    ok, note = _types_compatible(a, b)
                    if not ok:
                        rejected.append({**base, "reason": note})
                        continue
                    if note:
                        counter.append(note)
                        score += 0.05
                    else:
                        score += 0.15
                        evidence.append(f"same JSON type ({a.dominant})")
                    ok, bonus, note = _lengths_compatible(a, b)
                    if not ok:
                        rejected.append({**base, "reason": note})
                        continue
                    if note:
                        score += bonus
                        evidence.append(note)

                    if b_status == "unique_in_profile":
                        score += 0.15
                        evidence.append("target field is unique and complete in its dataset")
                    else:
                        score += 0.05
                        counter.append(f"target uniqueness not fully verified ({b_status})")

                    if a.distinct is not None and b.distinct is not None:
                        if a.distinct > b.distinct:
                            score -= 0.2
                            counter.append("source has more distinct values than the target key "
                                           "(orphans, or not a real reference)")
                        else:
                            score += 0.05
                            evidence.append("source distinct count <= target distinct count")
                    elif a.distinct is not None and b.distinct_capped:
                        evidence.append("source distinct count within target key range")

                    unique = a.unique_within_file
                    if a.in_array:
                        cardinality = "many-to-many (source holds an array of references)"
                    elif unique is True:
                        cardinality = "one-to-one"
                    elif unique is None:
                        cardinality = "unknown (source distinct count capped)"
                    else:
                        cardinality = "many-to-one"

                    pair = frozenset({(src.name, a.path), (tgt.name, b.path)})
                    if pair in seen_pairs:
                        continue  # reverse direction of an existing one-to-one candidate
                    seen_pairs.add(pair)

                    if a.missing_pct:
                        evidence.append(f"source missing in {a.missing_pct}% of records (optional link)")
                    score = round(max(min(score, 0.95), 0.0), 2)
                    candidates.append({
                        **base,
                        "status": "candidate",
                        "confidence": score,
                        "confidence_level": "high" if score >= 0.7 else "medium" if score >= 0.45 else "low",
                        "cardinality": cardinality,
                        "description": f"{src.entity}.{a.path} may reference {tgt.entity}.{b.path}",
                        "evidence": evidence,
                        "counter_evidence": counter,
                        "value_overlap_verified": False,
                    })

    candidates.sort(key=lambda c: (-c["confidence"], c["from"]["dataset"], c["from"]["field"]))
    for i, c in enumerate(candidates, start=1):
        c["id"] = f"R{i:03d}"
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in candidates:
        groups[(c["from"]["dataset"], c["from"]["field"])].append(c)
    for group in groups.values():
        for c in group:
            others = [o["id"] for o in group if o is not c]
            c["ambiguous"] = bool(others)
            c["alternative_ids"] = others
    return candidates, rejected


# ---------------------------------------------------------------------------
# Missing / ambiguous information
# ---------------------------------------------------------------------------
def collect_issues(datasets: list[Dataset], analyses: dict[str, dict], candidates: list[dict],
                   profile_issues: list[str]) -> list[dict[str, str]]:
    out = [{"scope": "profile", "issue": i} for i in profile_issues]
    out.append({"scope": "method", "issue": "Relationships are inferred from names, types and counts only; "
                "value overlap between files was not measured. Treat every relationship as unconfirmed."})
    for ds in datasets:
        a = analyses[ds.name]
        scope = ds.name
        if not ds.complete_scan:
            out.append({"scope": scope, "issue": "File was not fully scanned - statistics are partial."})
        if ds.invalid_lines:
            out.append({"scope": scope, "issue": f"{ds.invalid_lines:,} invalid lines in the source file."})
        if _int(ds.limits.get("fields_overflow_events")):
            out.append({"scope": scope, "issue": "Field cap reached - some field paths were not profiled "
                        "(possible dynamic keys). Re-run inspector with a higher --max-fields."})
        if _int(ds.limits.get("depth_truncations")):
            out.append({"scope": scope, "issue": "Nesting deeper than --max-depth was not profiled."})
        if not a["primary_key_candidates"]:
            out.append({"scope": scope, "issue": "No single-field primary-key candidate. The dataset may need a "
                        "composite key, or the ingestion content hash (_record_key) as its key."})
        elif len(a["primary_key_candidates"]) > 1:
            out.append({"scope": scope, "issue": f"{len(a['primary_key_candidates'])} primary-key candidates - "
                        "choose one in the mapping template."})
        unverified = [p["field"] for p in a["primary_key_candidates"] if p["status"] != "unique_in_profile"]
        if unverified:
            out.append({"scope": scope, "issue": f"Uniqueness not verifiable from the profile for: {unverified}. "
                        "Verify with COUNT(DISTINCT) in DuckDB after ingestion."})
        if not a["date_candidates"]:
            out.append({"scope": scope, "issue": "No date/time field detected."})
        for m in a["mixed_type_fields"]:
            out.append({"scope": scope, "issue": f"{m['field']}: mixed types {sorted(m['types'])}."})
        for amb in a["ambiguities"]:
            out.append({"scope": scope, "issue": amb})
        for f in a["identifier_candidates"]:
            if f["missing_pct"] and f["missing_pct"] >= HIGH_MISSING_PCT:
                out.append({"scope": scope, "issue": f"Identifier candidate {f['field']} is missing in "
                            f"{f['missing_pct']}% of records."})
    for c in candidates:
        if c["ambiguous"]:
            out.append({"scope": c["from"]["dataset"], "issue": f"{c['id']}: {c['from']['field']} matches several "
                        f"targets ({', '.join([c['id'], *c['alternative_ids']])}) - pick one."})
    if not candidates:
        out.append({"scope": "relationships", "issue": "No relationship candidates found from naming evidence. "
                    "Relationships may use non-descriptive field names; define them manually in the template."})
    return out


# ---------------------------------------------------------------------------
# Mapping template (merges your earlier confirmations)
# ---------------------------------------------------------------------------
def _rel_key(r: dict[str, Any]) -> tuple:
    f, t = r.get("from", {}), r.get("to", {})
    return (f.get("dataset"), f.get("field"), t.get("dataset"), t.get("field"))


def build_template(analyses: dict[str, dict], candidates: list[dict], previous: dict[str, Any] | None) -> dict:
    prev_ds = (previous or {}).get("datasets", {}) if isinstance(previous, dict) else {}
    prev_rels = (previous or {}).get("relationships", []) if isinstance(previous, dict) else []
    prev_by_key = {_rel_key(r): r for r in prev_rels if isinstance(r, dict)}

    datasets: dict[str, Any] = {}
    for name, a in analyses.items():
        old = prev_ds.get(name, {}) if isinstance(prev_ds.get(name), dict) else {}
        old_pk = old.get("primary_key") if isinstance(old.get("primary_key"), dict) else {}
        old_cols = {c.get("field"): c for c in old.get("columns", []) if isinstance(c, dict)}
        pk_alts = [p["field"] for p in a["primary_key_candidates"]]
        pk = {
            "field": old_pk.get("field") if old_pk.get("confirmed") else (pk_alts[0] if pk_alts else None),
            "confirmed": bool(old_pk.get("confirmed", False)),
            "candidates": a["primary_key_candidates"],
        }
        columns = []
        for f in a["fields"]:
            if f["in_array"] or f["depth"] > 0 and f["dominant_type"] in ("object", "array"):
                continue
            prev = old_cols.get(f["path"], {})
            columns.append({
                "field": f["path"],
                "role": f["role"],
                "suggested_type": f["suggested_sql_type"],
                "type": prev.get("type", f["suggested_sql_type"]),
                "include": bool(prev.get("include", False)),
            })
        datasets[name] = {
            "entity": a["entity"],
            "table": old.get("table", a["suggested_table"]),
            "valid_records": a["valid_records"],
            "primary_key": pk,
            "columns": columns,
            "notes": old.get("notes", ""),
        }

    rels = []
    for c in candidates:
        prev = prev_by_key.pop(_rel_key(c), {})
        rels.append({
            "id": c["id"],
            "from": c["from"],
            "to": c["to"],
            "cardinality": prev.get("cardinality", c["cardinality"]),
            "confidence": c["confidence"],
            "source": "analyzer",
            "confirmed": bool(prev.get("confirmed", False)),
            "notes": prev.get("notes", ""),
        })
    # Keep relationships you added (or analyzer ones you confirmed that no longer appear).
    for r in prev_by_key.values():
        if r.get("source") == "manual" or r.get("confirmed"):
            kept = dict(r)
            kept["source"] = r.get("source", "manual")
            kept.setdefault("notes", "")
            if r.get("source") != "manual":
                kept["notes"] = (kept["notes"] + " [no longer suggested by the analyzer - re-check]").strip()
            rels.append(kept)

    return {
        "_instructions": [
            "Edit this file to CONFIRM the schema. Only entries with \"confirmed\": true are used downstream.",
            "primary_key: set 'field' and 'confirmed': true once verified.",
            "relationships: set 'confirmed': true only after checking value overlap locally.",
            "Add your own relationships with \"source\": \"manual\" (same shape). They are kept on re-runs.",
            "columns: set 'include': true for fields to expose in typed views; adjust 'type' if needed.",
            "Re-running schema_analyzer.py keeps your confirmations, notes and column choices.",
        ],
        "template_version": TEMPLATE_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datasets": datasets,
        "relationships": rels,
    }


# ---------------------------------------------------------------------------
# Terminal report
# ---------------------------------------------------------------------------
def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n {title}\n{'=' * 78}")


def show_overview(analyses: dict[str, dict], candidates: list[dict], issues: list[dict]) -> None:
    _hr("DATASETS")
    print(f" {'#':>2}  {'dataset':<32} {'records':>12} {'fields':>7} {'nested':>7} {'ids':>4} {'pk':>3} {'dates':>6}")
    for i, a in enumerate(analyses.values(), start=1):
        flag = "" if a["complete_scan"] else "  (partial scan)"
        print(f" {i:>2}  {a['dataset']:<32} {a['valid_records']:>12,} {a['field_count']:>7} "
              f"{a['nested_field_count']:>7} {len(a['identifier_candidates']):>4} "
              f"{len(a['primary_key_candidates']):>3} {len(a['date_candidates']):>6}{flag}")
    levels = Counter(c["confidence_level"] for c in candidates)
    print(f"\n Relationship candidates: {len(candidates)} "
          f"(high {levels['high']}, medium {levels['medium']}, low {levels['low']}) - all UNCONFIRMED")
    print(f" Missing / ambiguous items: {len(issues)}")


def show_dataset(a: dict[str, Any]) -> None:
    _hr(f"DATASET  {a['dataset']}  ->  {a['suggested_table']}")
    print(f" records {a['valid_records']:,} | fields {a['field_count']} (top-level {a['top_level_field_count']}, "
          f"nested {a['nested_field_count']}, max depth {a['max_depth']}) | invalid lines {a['invalid_lines']:,}")
    print(f" roles: {', '.join(f'{k}={v}' for k, v in a['role_counts'].items())}")
    if a["object_fields"]:
        print(f" nested objects: {len(a['object_fields'])}   arrays: {len(a['array_fields'])}")
    print(f"\n {'field':<44} {'role':<20} {'type':<8} {'miss%':>6} {'distinct':>9}")
    for f in a["fields"]:
        print(f" {('  ' * f['depth'] + f['path'])[:44]:<44} {f['role'][:20]:<20} {str(f['dominant_type'])[:8]:<8} "
              f"{(f['missing_pct'] if f['missing_pct'] is not None else '-'):>6} {str(f['distinct_values']):>9}")


def show_identifiers(analyses: dict[str, dict]) -> None:
    _hr("IDENTIFIER & PRIMARY-KEY CANDIDATES")
    for a in analyses.values():
        print(f"\n {a['dataset']}")
        pks = {p["field"]: p["status"] for p in a["primary_key_candidates"]}
        if not a["identifier_candidates"]:
            print("   (none)")
        for c in a["identifier_candidates"]:
            pk = f"  PK? {pks[c['field']]}" if c["field"] in pks else ""
            print(f"   {c['score']:.2f}  {c['field']:<40} {str(c['dominant_type']):<8}{pk}")
            print(f"         - {'; '.join(c['evidence'])}")


def show_dates(analyses: dict[str, dict]) -> None:
    _hr("DATE / TIME CANDIDATES")
    for a in analyses.values():
        print(f"\n {a['dataset']}")
        if not a["date_candidates"]:
            print("   (none)")
        for d in a["date_candidates"]:
            print(f"   {d['confidence']:<6} {d['field']:<40} {d['kind']:<20} share={d['matching_share']:.0%} "
                  f"-> {d['suggested_sql_type']}")


def show_relationships(candidates: list[dict]) -> None:
    _hr("RELATIONSHIP CANDIDATES  (unconfirmed - evidence from names/types/counts only)")
    if not candidates:
        print(" (none found)")
    for c in candidates:
        amb = "  [AMBIGUOUS]" if c["ambiguous"] else ""
        print(f"\n {c['id']}  {c['confidence_level'].upper():<6} {c['confidence']:.2f}  {c['cardinality']}{amb}")
        print(f"   {c['from']['dataset']}.{c['from']['field']}  ->  {c['to']['dataset']}.{c['to']['field']}")
        for e in c["evidence"]:
            print(f"     + {e}")
        for e in c["counter_evidence"]:
            print(f"     - {e}")


def show_issues(issues: list[dict]) -> None:
    _hr("MISSING OR AMBIGUOUS INFORMATION")
    for i in issues:
        print(f" [{i['scope']}] {i['issue']}")


def interactive_menu(analyses: dict[str, dict], candidates: list[dict], issues: list[dict]) -> None:
    names = list(analyses)
    menu = ("\n 1 Datasets overview   2 Dataset detail   3 Identifiers/keys   4 Dates\n"
            " 5 Relationships       6 Missing/ambiguous   q Quit")
    while True:
        print(menu)
        try:
            choice = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("q", "quit", "exit"):
            return
        if choice == "1":
            show_overview(analyses, candidates, issues)
        elif choice == "2":
            for i, n in enumerate(names, start=1):
                print(f"   {i}. {n}")
            try:
                idx = int(input(" dataset # > ").strip()) - 1
                show_dataset(analyses[names[idx]])
            except (ValueError, IndexError, EOFError):
                print(" invalid selection")
        elif choice == "3":
            show_identifiers(analyses)
        elif choice == "4":
            show_dates(analyses)
        elif choice == "5":
            show_relationships(candidates)
        elif choice == "6":
            show_issues(issues)
        else:
            print(" unknown option")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Analyse the local data profile (never reads JSONL files).")
    p.add_argument("--profile", type=Path, default=PROFILE_PATH)
    p.add_argument("--output-dir", type=Path, default=config.REPORTS_DIR)
    p.add_argument("--force", action="store_true", help="Overwrite existing reports without asking (.bak kept).")
    p.add_argument("--no-write", action="store_true", help="Only show the report; write nothing.")
    p.add_argument("--no-interactive", action="store_true", help="Print the full report instead of a menu.")
    args = p.parse_args(argv)
    out_dir: Path = args.output_dir.resolve()

    try:
        meta, datasets, profile_issues = load_profile(args.profile.resolve(), out_dir)
    except ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    analyses = {ds.name: analyse_dataset(ds) for ds in datasets}
    candidates, rejected = find_relationships(datasets, analyses)
    issues = collect_issues(datasets, analyses, candidates, profile_issues)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    privacy = "Field names, types and aggregate counts only. No record values."

    summary = {"analyzer_version": ANALYZER_VERSION, "generated_at_utc": now, "privacy": privacy,
               **meta, "datasets": list(analyses.values()), "missing_or_ambiguous": issues}
    relationships = {
        "analyzer_version": ANALYZER_VERSION, "generated_at_utc": now, "privacy": privacy,
        "status": "ALL ENTRIES ARE CANDIDATES - NONE ARE CONFIRMED",
        "method": "Name, JSON-type, string-length and cardinality evidence from the profile. "
                  "Value overlap between datasets was NOT measured.",
        "candidate_count": len(candidates), "candidates": candidates,
        "rejected_name_matches": rejected,
    }

    show_overview(analyses, candidates, issues)

    if not args.no_write:
        paths = [out_dir / SUMMARY_NAME, out_dir / RELATIONSHIPS_NAME, out_dir / TEMPLATE_NAME]
        previous = None
        if paths[2].exists():
            try:
                previous = load_json_report(paths[2], what="Existing mapping template")
                print(f"\nExisting {TEMPLATE_NAME} found - your confirmations, notes and column choices will be kept.")
            except ReportError as exc:
                print(f"\nWARNING: {exc}. It will be backed up (.bak) and NOT merged.")
        template = build_template(analyses, candidates, previous)
        if confirm_overwrite(paths, force=args.force):
            out_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(paths[0], summary)
            write_json_atomic(paths[1], relationships)
            write_json_atomic(paths[2], template)
            print("\nWrote:")
            for path in paths:
                print(f"  {path}")
        else:
            print("\nNothing written.")

    if args.no_interactive or not is_interactive():
        for a in analyses.values():
            show_dataset(a)
        show_identifiers(analyses)
        show_dates(analyses)
        show_relationships(candidates)
        show_issues(issues)
    else:
        interactive_menu(analyses, candidates, issues)
    return 0


if __name__ == "__main__":
    sys.exit(main())
