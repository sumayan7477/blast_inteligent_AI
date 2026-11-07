"""
Tests for relationship_validator.py using SYNTHETIC data only.

Every dataset below is invented inside a temporary directory; no company data,
no project data/ directory and no project local_db/ are touched.

Run:  python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import config  # noqa: E402
import database as db  # noqa: E402
import relationship_validator as rv  # noqa: E402
from ingestion import default_table_name, load_mappings, run_ingestion  # noqa: E402

# --- synthetic datasets ------------------------------------------------------
PARENTS = [{"id": f"P{i}", "label": f"synthetic-{i}"} for i in range(1, 6)]           # P1..P5, unique
CHILDREN = [
    {"cid": 1, "parentId": "P1", "refs": ["P1", "P2"]},
    {"cid": 2, "parentId": "P1", "refs": ["P4", "Z1"]},   # Z1 = orphan array ref
    {"cid": 3, "parentId": "P2", "refs": []},
    {"cid": 4, "parentId": "P3"},
    {"cid": 5, "parentId": "X9"},                         # orphan
    {"cid": 6, "parentId": None},                         # null key
    {"cid": 7},                                           # missing key
    {"cid": 8, "parentId": ""},                           # empty key
]
DUP_PARENTS = [{"code": "A", "n": 1}, {"code": "A", "n": 2}, {"code": "B", "n": 3}]
ONE_CHILD = [{"pid": "P1", "meta": {"parent": "P5"}}, {"pid": "P2", "meta": {"parent": "P5"}}]
SECRET_VALUES = ("P1", "P2", "P3", "X9", "Z1", "synthetic-1")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _rel(rid: str, src: str, sfield: str, tgt: str, tfield: str, card: str = "many-to-one",
         confirmed: bool = True) -> dict:
    return {"id": rid, "from": {"dataset": src, "field": sfield}, "to": {"dataset": tgt, "field": tfield},
            "cardinality": card, "confirmed": confirmed, "source": "analyzer", "notes": ""}


class SyntheticDatabase(unittest.TestCase):
    """Builds a synthetic DuckDB through the real ingestion pipeline once per class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_synthetic_"))
        cls._orig_temp = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"   # keep DuckDB spill files out of the project
        data = cls.tmp / "data"
        data.mkdir()
        _write_jsonl(data / "parents.jsonl", PARENTS)
        _write_jsonl(data / "children.jsonl", CHILDREN)
        _write_jsonl(data / "dupparents.jsonl", DUP_PARENTS)
        _write_jsonl(data / "onechild.jsonl", ONE_CHILD)
        cls.db_path = cls.tmp / "synthetic.duckdb"
        mappings = load_mappings(cls.tmp / "no_config.json", data)
        rc = run_ingestion(mappings, data_dir=data, db_path=cls.db_path)
        assert rc == 0, "synthetic ingestion failed"

        names = ["parents.jsonl", "children.jsonl", "dupparents.jsonl", "onechild.jsonl"]
        cls.template = {
            "template_version": 1,
            "datasets": {
                n: {"entity": n.split(".")[0].upper(), "table": default_table_name(n), "columns": [],
                    "primary_key": {"field": None, "confirmed": False, "candidates": []}}
                for n in names
            },
            "relationships": [
                _rel("R001", "children.jsonl", "parentId", "parents.jsonl", "id"),
                _rel("R002", "children.jsonl", "refs[]", "parents.jsonl", "id", "many-to-many"),
                _rel("R003", "onechild.jsonl", "pid", "parents.jsonl", "id", "one-to-one"),
                _rel("R004", "children.jsonl", "parentId", "dupparents.jsonl", "code"),
                _rel("R005", "onechild.jsonl", "meta.parent", "parents.jsonl", "id", "one-to-one"),
                _rel("R006", "ghost.jsonl", "x", "parents.jsonl", "id"),
                _rel("R007", "children.jsonl", "parentId", "parents.jsonl", "does_not_exist"),
                _rel("R099", "onechild.jsonl", "pid", "parents.jsonl", "id", confirmed=False),
            ],
        }
        cls.template["datasets"]["parents.jsonl"]["primary_key"] = {"field": "id", "confirmed": True}
        cls.template["datasets"]["dupparents.jsonl"]["primary_key"] = {"field": "code", "confirmed": True}
        cls.template_path = cls.tmp / "schema_mapping_template.json"
        cls.template_path.write_text(json.dumps(cls.template, indent=2), encoding="utf-8")
        cls.opts = rv.Options()

    @classmethod
    def tearDownClass(cls) -> None:
        config.DB_TEMP_DIR = cls._orig_temp
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _validate(self, rid: str, opts: rv.Options | None = None) -> dict:
        rel = next(r for r in self.template["relationships"] if r["id"] == rid)
        with db.db_connection(self.db_path, read_only=True) as con:
            tables = rv.resolve_tables(con, self.template)
            return rv.validate_relationship(con, rel, tables, opts or self.opts)


class TestPathConversion(unittest.TestCase):
    def test_paths(self) -> None:
        self.assertEqual(rv.profile_path_to_json("id"), ('$."id"', False))
        self.assertEqual(rv.profile_path_to_json("a.b"), ('$."a"."b"', False))
        self.assertEqual(rv.profile_path_to_json("a[].b"), ('$."a"[*]."b"', True))
        self.assertEqual(rv.profile_path_to_json("refs[]"), ('$."refs"[*]', True))

    def test_rejects_unsafe(self) -> None:
        for bad in ("", "a..b", 'a"b', "a'b", "a\\b"):
            with self.assertRaises(ValueError):
                rv.profile_path_to_json(bad)


class TestRelationships(SyntheticDatabase):
    def test_orphans_nulls_duplicates(self) -> None:
        r = self._validate("R001")
        c, p, m = r["source_side"], r["target_side"], r["matching"]
        self.assertEqual(c["total_rows"], 8)
        self.assertEqual(c["rows_with_null_key"], 3)          # null, missing, empty string
        self.assertEqual(c["key_values"], 5)
        self.assertEqual(c["distinct_keys"], 4)
        self.assertEqual(c["duplicated_keys"], 1)
        self.assertTrue(p["is_unique"])
        self.assertEqual(p["total_rows"], 5)
        self.assertEqual(m["matched_values"], 4)
        self.assertEqual(m["unmatched_values"], 1)
        self.assertEqual(m["matched_distinct_keys"], 3)
        self.assertEqual(m["unmatched_distinct_keys"], 1)
        self.assertEqual(m["overlap_pct_values"], 80.0)
        self.assertEqual(m["orphan_records"], 1)
        self.assertEqual(m["referenced_parent_keys"], 3)
        self.assertEqual(m["unreferenced_parent_keys"], 2)
        self.assertEqual(m["max_children_per_parent"], 2)
        self.assertEqual(r["observed_cardinality"], "many-to-one")
        self.assertEqual(r["status"], rv.FAIL)               # 80% < default 90% threshold
        self.assertTrue(r["human_review_required"])

    def test_threshold_downgrades_to_warning(self) -> None:
        r = self._validate("R001", rv.Options(warn_threshold_pct=75.0))
        self.assertEqual(r["status"], rv.WARN)

    def test_array_references(self) -> None:
        r = self._validate("R002")
        c, m = r["source_side"], r["matching"]
        self.assertTrue(c["is_array_path"])
        self.assertEqual(c["key_values"], 4)
        self.assertEqual(c["rows_with_key"], 2)
        self.assertEqual(m["matched_values"], 3)
        self.assertEqual(m["unmatched_values"], 1)
        self.assertEqual(m["orphan_records"], 1)
        self.assertTrue(r["observed_cardinality"].startswith("many-to-many"))

    def test_clean_one_to_one_passes_pending_review(self) -> None:
        r = self._validate("R003")
        self.assertEqual(r["status"], rv.PASS)
        self.assertEqual(r["matching"]["overlap_pct_values"], 100.0)
        self.assertEqual(r["observed_cardinality"], "one-to-one")
        self.assertTrue(r["cardinality_consistent"])

    def test_non_unique_target_fails(self) -> None:
        r = self._validate("R004")
        self.assertFalse(r["target_side"]["is_unique"])
        self.assertEqual(r["target_side"]["duplicated_keys"], 1)
        self.assertEqual(r["status"], rv.FAIL)

    def test_nested_object_path(self) -> None:
        r = self._validate("R005")
        self.assertEqual(r["matching"]["matched_values"], 2)
        self.assertEqual(r["observed_cardinality"], "many-to-one")
        self.assertFalse(r["cardinality_consistent"])          # declared one-to-one
        self.assertEqual(r["status"], rv.WARN)

    def test_missing_table_is_error(self) -> None:
        r = self._validate("R006")
        self.assertEqual(r["status"], rv.ERROR)

    def test_missing_field_is_error(self) -> None:
        r = self._validate("R007")
        self.assertEqual(r["status"], rv.ERROR)

    def test_primary_keys(self) -> None:
        with db.db_connection(self.db_path, read_only=True) as con:
            tables = rv.resolve_tables(con, self.template)
            good = rv.validate_primary_key(con, "parents.jsonl", "id", tables, self.opts)
            dup = rv.validate_primary_key(con, "dupparents.jsonl", "code", tables, self.opts)
            nulls = rv.validate_primary_key(con, "children.jsonl", "parentId", tables, self.opts)
        self.assertEqual(good["status"], rv.PASS)
        self.assertEqual(dup["status"], rv.FAIL)
        self.assertEqual(nulls["status"], rv.FAIL)


class TestSelectionAndCli(SyntheticDatabase):
    def test_default_selects_confirmed_only(self) -> None:
        ids = [r["id"] for r in rv.select_relationships(self.template, None, False)]
        self.assertNotIn("R099", ids)
        self.assertIn("R099", [r["id"] for r in rv.select_relationships(self.template, None, True)])

    def test_unconfirmed_id_requires_flag(self) -> None:
        with self.assertRaises(rv.ValidationSetupError):
            rv.select_relationships(self.template, ["R099"], False)
        self.assertEqual(len(rv.select_relationships(self.template, ["R099"], True)), 1)
        with self.assertRaises(rv.ValidationSetupError):
            rv.select_relationships(self.template, ["NOPE"], True)

    def test_cli_writes_reports_without_values_and_never_edits_template(self) -> None:
        out = self.tmp / "reports"
        before = self.template_path.read_bytes()
        rc = rv.main(["--template", str(self.template_path), "--db", str(self.db_path),
                      "--output-dir", str(out), "--force"])
        self.assertEqual(rc, 1)  # synthetic set intentionally contains failures
        self.assertEqual(self.template_path.read_bytes(), before)
        report_json = (out / rv.JSON_REPORT).read_text(encoding="utf-8")
        report_md = (out / rv.MD_REPORT).read_text(encoding="utf-8")
        report = json.loads(report_json)
        self.assertTrue(report["human_review_required"])
        self.assertEqual(report["summary"]["relationships_validated"], 7)  # confirmed only
        for value in SECRET_VALUES:
            self.assertNotIn(f'"{value}"', report_json, f"value {value} leaked into JSON report")
            self.assertNotIn(f" {value} ", report_md, f"value {value} leaked into Markdown report")

    def test_single_relationship_option(self) -> None:
        out = self.tmp / "reports_single"
        rc = rv.main(["--template", str(self.template_path), "--db", str(self.db_path),
                      "--output-dir", str(out), "--force", "-r", "R003"])
        self.assertEqual(rc, 0)
        report = json.loads((out / rv.JSON_REPORT).read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in report["relationships"]], ["R003"])

    def test_missing_database(self) -> None:
        rc = rv.main(["--template", str(self.template_path), "--db", str(self.tmp / "absent.duckdb"),
                      "--output-dir", str(self.tmp / "x"), "--force"])
        self.assertEqual(rc, 2)

    def test_missing_and_malformed_template(self) -> None:
        self.assertEqual(rv.main(["--template", str(self.tmp / "absent.json"), "--db", str(self.db_path)]), 2)
        bad = self.tmp / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        self.assertEqual(rv.main(["--template", str(bad), "--db", str(self.db_path)]), 2)


if __name__ == "__main__":
    unittest.main()
