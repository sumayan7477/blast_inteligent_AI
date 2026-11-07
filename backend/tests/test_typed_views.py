"""
Tests for typed_views.py using SYNTHETIC data only.

All datasets are invented in a temporary directory. The real data/ directory,
local_db/ and local_reports/ are never touched.

Run:  python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import copy
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
import typed_views as tv  # noqa: E402
from ingestion import default_table_name, load_mappings, run_ingestion  # noqa: E402

SITES = [
    {"siteId": "S1", "name": "North", "elevation": "120.5", "active": "true", "opened": "2024-01-31", "survey": "05/03/2024"},
    {"siteId": "S2", "name": "South", "elevation": "abc", "active": "false", "opened": "2024-02-15", "survey": "06/03/2024"},
    {"siteId": "S3", "name": "East", "elevation": 98, "active": "maybe", "opened": "31/12/2023", "survey": "07/03/2024"},
]
RIGS = [
    {"rigId": 1, "site": "S1", "depthM": 12.5, "holes": "3", "shift": {"start": 1700000000}, "loggedAt": "2024-02-01T08:00:00Z"},
    {"rigId": 2, "site": "S1", "depthM": "7", "holes": 4, "shift": {"start": 1700003600}, "loggedAt": "2024-02-01T09:00:00+02:00"},
    {"rigId": 3, "site": "S2", "depthM": "NaN", "holes": "3.5", "shift": {"start": 1e20}, "loggedAt": "not-a-date"},
    {"rigId": 4, "site": None, "depthM": 9.0, "holes": 5.0, "shift": {}, "loggedAt": None},
]
EVENTS = [{"eventId": "E1", "rig": "1"}, {"eventId": "E2", "rig": "2"}, {"eventId": "E3", "rig": "99"}]
SYNTHETIC_VALUES = ("North", "South", "S1", "S2", "E1", "120.5", "not-a-date")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _col(field: str, type_: str, include: bool = True, **extra) -> dict:
    return {"field": field, "role": "synthetic", "suggested_type": type_, "type": type_, "include": include, **extra}


def _rel(rid: str, src: str, sf: str, tgt: str, tf: str, confirmed: bool = True) -> dict:
    return {"id": rid, "from": {"dataset": src, "field": sf}, "to": {"dataset": tgt, "field": tf},
            "cardinality": "many-to-one", "confirmed": confirmed, "source": "analyzer", "notes": ""}


def _template() -> dict:
    def ds(name: str, pk: str, cols: list[dict]) -> dict:
        return {"entity": name.split(".")[0].upper(), "table": default_table_name(name),
                "primary_key": {"field": pk, "confirmed": True, "candidates": []}, "columns": cols, "notes": ""}
    return {
        "template_version": 1,
        "datasets": {
            "sites.jsonl": ds("sites.jsonl", "siteId", [
                _col("siteId", "VARCHAR", include=False), _col("name", "VARCHAR"), _col("elevation", "DOUBLE"),
                _col("active", "BOOLEAN"), _col("opened", "DATE"), _col("survey", "DATE", format="%d/%m/%Y")]),
            "rigs.jsonl": ds("rigs.jsonl", "rigId", [
                _col("rigId", "BIGINT", include=False), _col("site", "VARCHAR", include=False),
                _col("depthM", "DOUBLE"), _col("holes", "INTEGER"), _col("shift.start", "EPOCH_S"),
                _col("loggedAt", "TIMESTAMPTZ"), _col("ghostField", "VARCHAR")]),
            "events.jsonl": ds("events.jsonl", "eventId", [_col("eventId", "VARCHAR"), _col("rig", "VARCHAR")]),
        },
        "relationships": [
            _rel("R001", "rigs.jsonl", "site", "sites.jsonl", "siteId"),            # validates clean -> joined
            _rel("R002", "events.jsonl", "rig", "rigs.jsonl", "rigId"),             # orphan -> failed -> not joined
            _rel("R004", "events.jsonl", "rig", "rigs.jsonl", "rigId", confirmed=False),  # unconfirmed -> ignored
        ],
    }


class TypedViewsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_tv_synthetic_"))
        cls._orig_temp = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"
        data = cls.tmp / "data"
        data.mkdir()
        _write_jsonl(data / "sites.jsonl", SITES)
        _write_jsonl(data / "rigs.jsonl", RIGS)
        _write_jsonl(data / "events.jsonl", EVENTS)
        cls.pristine_db = cls.tmp / "pristine.duckdb"
        assert run_ingestion(load_mappings(cls.tmp / "none.json", data), data_dir=data, db_path=cls.pristine_db) == 0

        cls.reports = cls.tmp / "reports"
        cls.template = _template()
        cls.template_path = cls.tmp / "schema_mapping_template.json"
        cls.template_path.write_text(json.dumps(cls.template), encoding="utf-8")
        rv.main(["--template", str(cls.template_path), "--db", str(cls.pristine_db),
                 "--output-dir", str(cls.reports), "--force", "--skip-primary-keys"])
        cls.validation_path = cls.reports / rv.JSON_REPORT
        # R003 is confirmed AFTER validation -> must be skipped as "not validated".
        cls.template["relationships"].append(_rel("R003", "events.jsonl", "eventId", "sites.jsonl", "siteId"))
        cls.template_path.write_text(json.dumps(cls.template), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        config.DB_TEMP_DIR = cls._orig_temp
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh_db(self, name: str) -> Path:
        path = self.tmp / f"{name}.duckdb"
        shutil.copy(self.pristine_db, path)
        return path

    def run_tv(self, db_path: Path, *extra: str, template: Path | None = None) -> tuple[int, dict]:
        out = self.tmp / f"out_{db_path.stem}"
        rc = tv.main(["--template", str(template or self.template_path), "--validation", str(self.validation_path),
                      "--db", str(db_path), "--output-dir", str(out), "--force", *extra])
        report = json.loads((out / tv.JSON_REPORT).read_text(encoding="utf-8"))
        report["_md"] = (out / tv.MD_REPORT).read_text(encoding="utf-8")
        report["_raw_json"] = (out / tv.JSON_REPORT).read_text(encoding="utf-8")
        return rc, report

    @staticmethod
    def views(db_path: Path) -> set[str]:
        with db.db_connection(db_path, read_only=True) as con:
            return set(db.list_tables(con, table_type="VIEW"))

    @staticmethod
    def raw_fingerprint(db_path: Path) -> dict[str, tuple]:
        with db.db_connection(db_path, read_only=True) as con:
            return {t: con.execute(f"SELECT count(*), sum(hash(payload)), sum(hash(_record_key)) FROM {t}").fetchone()
                    for t in db.list_raw_tables(con)}


def _ds(report: dict, view: str) -> dict:
    return next(d for d in report["dataset_views"] if d["view"] == view)


def _c(dataset: dict, column: str) -> dict:
    return next(c for c in dataset["columns"] if c["column"] == column)


class TestNamingAndPlanning(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(tv.column_name_for("shift.start"), "shift_start")
        self.assertEqual(tv.column_name_for("9lives"), "c_9lives")
        self.assertEqual(tv.dataset_view_name("rigs.jsonl", "raw_rigs"), "tv_rigs")
        self.assertLessEqual(len(tv.join_view_name("R001", "tv_" + "a" * 60, "tv_" + "b" * 60)), 63)

    def test_duplicate_column_names_are_rejected(self) -> None:
        t = _template()
        t["datasets"]["sites.jsonl"]["columns"].append(_col("alias", "VARCHAR", column_name="name"))
        plans, _, _ = tv.build_plan(t, None, {}, allow_warnings=False)
        sites = next(p for p in plans if p.dataset == "sites.jsonl")
        self.assertTrue(any("duplicate column name" in e for e in sites.errors))

    def test_duplicate_view_names_are_rejected(self) -> None:
        t = _template()
        t["datasets"]["rigs.jsonl"]["view_name"] = "tv_sites"
        plans, joins, _ = tv.build_plan(t, None, {}, allow_warnings=False)
        self.assertTrue(any("duplicate view name" in e for e in tv.check_view_names(plans, joins)))

    def test_invalid_type_and_array_field(self) -> None:
        t = _template()
        t["datasets"]["events.jsonl"]["columns"] += [_col("amount", "MONEY"),
                                                     _col("tags[]", "VARCHAR")]
        plans, _, _ = tv.build_plan(t, None, {}, allow_warnings=False)
        ev = next(p for p in plans if p.dataset == "events.jsonl")
        self.assertTrue(any("type must be one of" in e for e in ev.errors))
        self.assertTrue(any("inside arrays" in e for e in ev.errors))


class TestCreateViews(TypedViewsBase):
    def test_dry_run_creates_nothing(self) -> None:
        path = self.fresh_db("dry")
        rc, report = self.run_tv(path, "--dry-run")
        self.assertFalse(report["views_created"])
        self.assertFalse(any(v.startswith(("tv_", "tj_")) for v in self.views(path)))
        self.assertEqual(_c(_ds(report, "tv_sites"), "elevation")["conversion_failures"], 1)

    def test_create_conversions_joins_and_raw_preserved(self) -> None:
        path = self.fresh_db("create")
        before = self.raw_fingerprint(path)
        rc, report = self.run_tv(path)
        self.assertEqual(rc, 1)  # ghostField intentionally missing -> excluded -> non-zero exit
        self.assertTrue(report["views_created"])
        self.assertEqual(self.raw_fingerprint(path), before, "raw tables must be unchanged")
        views = self.views(path)
        self.assertTrue({"tv_sites", "tv_rigs", "tv_events", "tj_r001_rigs_to_sites"} <= views)

        sites, rigs = _ds(report, "tv_sites"), _ds(report, "tv_rigs")
        self.assertEqual(sites["view_rows"], 3)
        self.assertEqual(_c(sites, "elevation")["conversion_failures"], 1)      # "abc"
        self.assertEqual(_c(sites, "active")["conversion_failures"], 1)         # "maybe"
        self.assertEqual(_c(sites, "opened")["conversion_failures"], 1)         # dd/mm/yyyy without format
        self.assertEqual(_c(sites, "survey")["conversion_failures"], 0)         # explicit format
        self.assertEqual(_c(sites, "siteId")["origin"], "primary_key")
        self.assertEqual(_c(rigs, "holes")["conversion_failures"], 1)           # "3.5" is NOT rounded
        self.assertEqual(_c(rigs, "depthM")["conversion_failures"], 1)          # "NaN"
        self.assertEqual(_c(rigs, "shift_start")["conversion_failures"], 1)     # out-of-range epoch, no crash
        self.assertEqual(_c(rigs, "shift_start")["missing"], 1)
        self.assertEqual(_c(rigs, "loggedAt")["conversion_failures"], 1)
        self.assertEqual(_c(rigs, "site")["origin"], "relationship_key")
        self.assertEqual(_c(rigs, "ghostField")["status"], "error")

        with db.db_connection(path, read_only=True) as con:
            cols = [r[0] for r in con.execute("DESCRIBE tv_rigs").fetchall()]
            self.assertNotIn("ghostField", cols)
            self.assertIn("_record_key", cols)
            types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                     "WHERE table_name = 'tv_rigs'").fetchall())
            self.assertEqual(types["holes"], "INTEGER")
            self.assertEqual(types["depthM"], "DOUBLE")
            self.assertTrue(types["shift_start"].startswith("TIMESTAMP"))
            self.assertEqual(con.execute("SELECT count(holes) FROM tv_rigs").fetchone()[0], 3)

        join = next(j for j in report["join_views"] if j["relationship_id"] == "R001")
        self.assertEqual((join["status"], join["rows"], join["matched_rows"], join["unmatched_rows"]),
                         ("created", 4, 3, 1))
        not_joined = {s["id"]: s["reason"] for s in report["relationships_not_joined"]}
        self.assertIn("failed", not_joined["R002"])
        self.assertIn("not found", not_joined["R003"])
        self.assertNotIn("R004", not_joined)   # unconfirmed: never considered

        for value in SYNTHETIC_VALUES:
            self.assertNotIn(f'"{value}"', report["_raw_json"], f"value {value} leaked into JSON report")
            self.assertNotIn(value, report["_md"], f"value {value} leaked into Markdown report")

    def test_rerun_is_idempotent(self) -> None:
        path = self.fresh_db("rerun")
        self.run_tv(path)
        views_first = self.views(path)
        rc, report = self.run_tv(path)
        self.assertTrue(report["views_created"])
        self.assertEqual(self.views(path), views_first)
        with db.db_connection(path, read_only=True) as con:
            n, distinct = con.execute(f"SELECT count(*), count(DISTINCT view_name) FROM {tv.REGISTRY_TABLE}").fetchone()
        self.assertEqual(n, distinct)

    def test_strict_mode_creates_nothing_on_errors(self) -> None:
        path = self.fresh_db("strict")
        rc, report = self.run_tv(path, "--strict")
        self.assertEqual(rc, 1)
        self.assertFalse(report["views_created"])
        self.assertFalse(any(v.startswith(("tv_", "tj_")) for v in self.views(path)))

    def test_allow_warnings_still_requires_accepted_status(self) -> None:
        path = self.fresh_db("warn")
        _, report = self.run_tv(path, "--dry-run", "--allow-warnings")
        reasons = {s["id"]: s["reason"] for s in report["relationships_not_joined"]}
        self.assertIn("R002", reasons)   # 'failed' is never joined

    def test_prune_drops_only_managed_views(self) -> None:
        path = self.fresh_db("prune")
        self.run_tv(path)
        t = copy.deepcopy(self.template)
        ev = t["datasets"]["events.jsonl"]
        ev["primary_key"]["confirmed"] = False
        for c in ev["columns"]:
            c["include"] = False
        t["relationships"] = [r for r in t["relationships"] if r["id"] == "R001"]
        tpath = self.tmp / "template_pruned.json"
        tpath.write_text(json.dumps(t), encoding="utf-8")
        self.run_tv(path, "--prune", template=tpath)
        views = self.views(path)
        self.assertNotIn("tv_events", views)
        self.assertIn("tv_sites", views)
        with db.db_connection(path, read_only=True) as con:
            self.assertIn("raw_events", db.list_tables(con))

    def test_unmanaged_objects_are_never_replaced(self) -> None:
        path = self.fresh_db("conflict")
        with db.db_connection(path) as con:
            con.execute("CREATE VIEW tv_sites AS SELECT 1 AS x")
            con.execute("CREATE TABLE tv_events (x INTEGER)")
        rc, report = self.run_tv(path)
        self.assertEqual(rc, 1)
        self.assertFalse(report["views_created"])
        self.assertTrue(any("not created by typed_views.py" in e for e in report["errors"]))
        self.assertTrue(any("as a TABLE" in e for e in report["errors"]))
        with db.db_connection(path, read_only=True) as con:
            self.assertIn("tv_events", db.list_tables(con))   # the table is untouched

    def test_missing_inputs(self) -> None:
        self.assertEqual(tv.main(["--template", str(self.tmp / "absent.json"), "--db", str(self.pristine_db)]), 2)
        self.assertEqual(tv.main(["--template", str(self.template_path), "--db", str(self.tmp / "absent.duckdb"),
                                  "--validation", str(self.validation_path)]), 2)

    def test_missing_validation_report_means_no_joins(self) -> None:
        path = self.fresh_db("noval")
        out = self.tmp / "out_noval"
        tv.main(["--template", str(self.template_path), "--validation", str(self.tmp / "absent.json"),
                 "--db", str(path), "--output-dir", str(out), "--force"])
        report = json.loads((out / tv.JSON_REPORT).read_text(encoding="utf-8"))
        self.assertTrue(report["views_created"])           # dataset views still built
        self.assertFalse(any(v.startswith("tj_") for v in self.views(path)))


if __name__ == "__main__":
    unittest.main()
