"""
Render a Mermaid ER diagram from the mapping template you have confirmed.

Input : ``local_reports/schema_mapping_template.json`` (edited by you)
        ``local_reports/schema_summary.json``         (optional - used to validate field paths)
Output: ``local_reports/database_relationships.md``

Section 1 contains ONLY confirmed items:
  * primary keys with ``"confirmed": true``
  * relationships with ``"confirmed": true``
  * columns with ``"include": true``
Section 2 shows unconfirmed candidates in a separate diagram with dotted
lines and no attributes, clearly labelled as not part of the schema.
Confirmed entries that reference unknown datasets/fields are excluded and
listed under "Validation problems".

Reads no JSONL data and writes no record values.

Usage
-----
    python backend/schema_visualizer.py
    python backend/schema_visualizer.py --confirmed-only     # omit the candidates section
    python backend/schema_visualizer.py --force              # overwrite without asking
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from report_io import ReportError, confirm_overwrite, load_json_report, write_text_atomic  # noqa: E402

TEMPLATE_PATH = config.REPORTS_DIR / "schema_mapping_template.json"
SUMMARY_PATH = config.REPORTS_DIR / "schema_summary.json"
OUTPUT_PATH = config.REPORTS_DIR / "database_relationships.md"

_MERMAID_TYPES = {
    "VARCHAR": "string", "BIGINT": "int", "INTEGER": "int", "DOUBLE": "float", "BOOLEAN": "boolean",
    "DATE": "date", "TIMESTAMP": "datetime", "TIMESTAMPTZ": "datetime", "EPOCH_S": "datetime",
    "EPOCH_MS": "datetime", "JSON": "json",
}
# (left cardinality, right cardinality) with the TARGET ("to") on the left.
_CARDINALITY = {
    "many-to-one": ("||", "o{"),
    "one-to-one": ("||", "o|"),
}
_DEFAULT_CARDINALITY = ("}o", "o{")


def _ident(name: str) -> str:
    """Make a Mermaid-safe identifier."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip("_") or "x"
    return f"f_{s}" if s[0].isdigit() else s


def _label(text: str) -> str:
    return re.sub(r'["\n\r{}|<>]', "", str(text))[:80]


def _edge(cardinality: str, dotted: bool) -> str:
    left, right = next(
        (v for k, v in _CARDINALITY.items() if str(cardinality).startswith(k)), _DEFAULT_CARDINALITY
    )
    return f"{left}{'..' if dotted else '--'}{right}"


class Template:
    """Validated view of the mapping template."""

    def __init__(self, raw: Any, known_fields: dict[str, set[str]] | None) -> None:
        self.problems: list[str] = []
        if not isinstance(raw, dict) or not isinstance(raw.get("datasets"), dict):
            raise ReportError("Mapping template has an unexpected structure (expected a 'datasets' object).")
        self.datasets: dict[str, dict[str, Any]] = {
            k: v for k, v in raw["datasets"].items() if isinstance(v, dict) and not k.startswith("_")
        }
        rels = raw.get("relationships", [])
        self.relationships: list[dict[str, Any]] = [r for r in rels if isinstance(r, dict)] if isinstance(rels, list) else []
        if isinstance(rels, list) and len(self.relationships) != len(rels):
            self.problems.append(f"{len(rels) - len(self.relationships)} malformed relationship entries ignored.")

        # Fields known per dataset: schema_summary (authoritative) or template columns + PK candidates.
        self.fields: dict[str, set[str]] = {}
        for name, ds in self.datasets.items():
            if known_fields and name in known_fields:
                self.fields[name] = known_fields[name]
            else:
                cols = {c.get("field") for c in ds.get("columns", []) if isinstance(c, dict)}
                pk = ds.get("primary_key") if isinstance(ds.get("primary_key"), dict) else {}
                cols |= {c.get("field") for c in pk.get("candidates", []) if isinstance(c, dict)}
                self.fields[name] = {c for c in cols if isinstance(c, str)}

    def entity(self, dataset: str) -> str:
        ds = self.datasets.get(dataset, {})
        return _ident(ds.get("entity") or Path(dataset).stem.upper())

    def confirmed_pk(self, dataset: str) -> str | None:
        pk = self.datasets.get(dataset, {}).get("primary_key")
        if isinstance(pk, dict) and pk.get("confirmed") is True and isinstance(pk.get("field"), str):
            if pk["field"] in self.fields.get(dataset, set()):
                return pk["field"]
            self.problems.append(f"{dataset}: confirmed primary key '{pk['field']}' is not a known field - ignored.")
        return None

    def included_columns(self, dataset: str) -> list[dict[str, Any]]:
        cols = self.datasets.get(dataset, {}).get("columns", [])
        return [c for c in cols if isinstance(c, dict) and c.get("include") is True and isinstance(c.get("field"), str)]

    def _endpoint_ok(self, rel: dict[str, Any], side: str) -> bool:
        end = rel.get(side)
        rid = rel.get("id", "?")
        if not isinstance(end, dict) or not isinstance(end.get("dataset"), str) or not isinstance(end.get("field"), str):
            self.problems.append(f"{rid}: '{side}' must have 'dataset' and 'field'.")
            return False
        if end["dataset"] not in self.datasets:
            self.problems.append(f"{rid}: unknown dataset '{end['dataset']}' in '{side}'.")
            return False
        if end["field"] not in self.fields.get(end["dataset"], set()):
            self.problems.append(f"{rid}: unknown field '{end['field']}' in {end['dataset']}.")
            return False
        return True

    def split_relationships(self) -> tuple[list[dict], list[dict]]:
        confirmed, candidates = [], []
        for r in self.relationships:
            if r.get("confirmed") is True:
                if self._endpoint_ok(r, "from") and self._endpoint_ok(r, "to"):
                    confirmed.append(r)
            elif self._endpoint_ok(r, "from") and self._endpoint_ok(r, "to"):
                candidates.append(r)
        return confirmed, candidates


def _known_fields_from_summary(path: Path) -> tuple[dict[str, set[str]] | None, str | None]:
    if not path.exists():
        return None, "schema_summary.json not found - fields validated against the template only."
    try:
        summary = load_json_report(path, what="Schema summary")
    except ReportError as exc:
        return None, f"{exc} - fields validated against the template only."
    out: dict[str, set[str]] = {}
    for ds in summary.get("datasets", []) if isinstance(summary, dict) else []:
        if isinstance(ds, dict) and isinstance(ds.get("dataset"), str):
            out[ds["dataset"]] = {f.get("path") for f in ds.get("fields", []) if isinstance(f, dict)}
    return out or None, None


def render(t: Template, *, include_candidates: bool, max_columns: int) -> tuple[str, dict[str, int]]:
    confirmed, candidates = t.split_relationships()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Database relationships",
        "",
        f"_Generated {now} from `schema_mapping_template.json`. Local only - field names, no record values._",
        "",
        "## 1. Confirmed schema",
        "",
        "Only primary keys and relationships marked `\"confirmed\": true`, and columns marked "
        "`\"include\": true`, appear here.",
        "",
    ]

    # Attributes per entity
    attrs: dict[str, list[str]] = {}
    fk_fields: dict[str, set[str]] = {}
    for r in confirmed:
        fk_fields.setdefault(r["from"]["dataset"], set()).add(r["from"]["field"])
    pks = {name: t.confirmed_pk(name) for name in t.datasets}

    for name in t.datasets:
        rows: list[str] = []
        col_types = {c.get("field"): c.get("type") for c in t.datasets[name].get("columns", []) if isinstance(c, dict)}
        wanted: list[tuple[str, str]] = []
        if pks[name]:
            wanted.append((pks[name], "PK"))
        for f in sorted(fk_fields.get(name, set())):
            if f != pks[name]:
                wanted.append((f, "FK"))
        keyed = {f for f, _ in wanted}
        included = [c for c in t.included_columns(name) if c["field"] not in keyed]
        for c in included[:max_columns]:
            wanted.append((c["field"], ""))
        for f, key in wanted:
            mtype = _MERMAID_TYPES.get(str(col_types.get(f) or "VARCHAR").upper(), "string")
            comment = f' "{_label(f)}"' if _ident(f) != f else ""
            if key == "PK" and f in fk_fields.get(name, set()):
                comment = f' "{_label(f)} - also FK"'
            rows.append(f"        {mtype} {_ident(f)}{(' ' + key) if key else ''}{comment}")
        if len(included) > max_columns:
            rows.append(f'        string more_columns "{len(included) - max_columns} more included columns"')
        if rows:
            attrs[t.entity(name)] = rows

    if attrs or confirmed:
        lines += ["```mermaid", "erDiagram"]
        for ent in sorted(attrs):  # entities without attributes are declared by their relationship lines
            lines += [f"    {ent} {{", *attrs[ent], "    }"]
        for r in confirmed:
            label = _label(f"{r['from']['field']} to {r['to']['field']}")
            lines.append(f"    {t.entity(r['to']['dataset'])} {_edge(r.get('cardinality', ''), False)} "
                         f"{t.entity(r['from']['dataset'])} : \"{label}\"")
        lines += ["```", ""]
    else:
        lines += ["> No confirmed mappings yet. Edit `local_reports/schema_mapping_template.json`, set",
                  "> `\"confirmed\": true` on verified primary keys/relationships, then re-run this script.", ""]

    if confirmed:
        lines += ["| id | from | to | cardinality | notes |", "|---|---|---|---|---|"]
        for r in confirmed:
            lines.append(f"| {_label(r.get('id', ''))} | {_label(r['from']['dataset'])}.{_label(r['from']['field'])} "
                         f"| {_label(r['to']['dataset'])}.{_label(r['to']['field'])} | {_label(r.get('cardinality', ''))} "
                         f"| {_label(r.get('notes', ''))} |")
        lines.append("")

    if include_candidates:
        lines += [
            "## 2. Unconfirmed candidates - NOT part of the schema",
            "",
            "Dotted lines. Inferred from field names, types and counts only; value overlap was not measured.",
            "",
        ]
        if candidates:
            lines += ["```mermaid", "erDiagram"]
            for r in candidates:
                conf = r.get("confidence")
                label = _label(f"{r.get('id', '?')} candidate" + (f" {conf:.2f}" if isinstance(conf, (int, float)) else ""))
                lines.append(f"    {t.entity(r['to']['dataset'])} {_edge(r.get('cardinality', ''), True)} "
                             f"{t.entity(r['from']['dataset'])} : \"{label}\"")
            lines += ["```", "", "| id | confidence | from | to | cardinality |", "|---|---|---|---|---|"]
            for r in candidates:
                lines.append(f"| {_label(r.get('id', ''))} | {r.get('confidence', '')} "
                             f"| {_label(r['from']['dataset'])}.{_label(r['from']['field'])} "
                             f"| {_label(r['to']['dataset'])}.{_label(r['to']['field'])} | {_label(r.get('cardinality', ''))} |")
            lines.append("")
        else:
            lines += ["_No unconfirmed candidates._", ""]

    if t.problems:
        lines += ["## 3. Validation problems", ""] + [f"- {_label(p)}" for p in t.problems] + [""]

    stats = {"confirmed_relationships": len(confirmed), "candidate_relationships": len(candidates),
             "confirmed_primary_keys": sum(1 for v in pks.values() if v), "problems": len(t.problems)}
    return "\n".join(lines), stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mermaid ER diagram from confirmed schema mappings.")
    p.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    p.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    p.add_argument("--output", type=Path, default=OUTPUT_PATH)
    p.add_argument("--confirmed-only", action="store_true", help="Omit the unconfirmed candidates section.")
    p.add_argument("--max-columns", type=int, default=25, help="Max included columns drawn per entity.")
    p.add_argument("--force", action="store_true", help="Overwrite the output without asking (.bak kept).")
    args = p.parse_args(argv)

    try:
        raw = load_json_report(args.template.resolve(), what="Mapping template")
        known, note = _known_fields_from_summary(args.summary.resolve())
        template = Template(raw, known)
    except ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if "not found" in str(exc):
            print("Run first:  python backend\\schema_analyzer.py", file=sys.stderr)
        return 2
    if note:
        template.problems.append(note)

    markdown, stats = render(template, include_candidates=not args.confirmed_only,
                             max_columns=max(args.max_columns, 0))
    out = args.output.resolve()
    if not confirm_overwrite([out], force=args.force):
        print("Nothing written.")
        return 1
    write_text_atomic(out, markdown + "\n")
    print(f"Wrote {out}")
    print("  " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items()))
    for prob in template.problems:
        print(f"  WARNING: {prob}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
