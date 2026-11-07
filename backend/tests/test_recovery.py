"""
Tests for recovery.py (strict automatic decisions + standalone demo) using SYNTHETIC data only.

Full pipeline in a temp directory:
  synthetic JSONL -> data_inspector -> ingestion -> schema_analyzer -> recovery validate -> recovery prepare-demo

The synthetic data is built so that each policy rule is exercised:
  holes.blastId -> blasts.blastId          clean many-to-one          => APPROVED
  holes.siteCode -> sites / site_archive   both fit                   => ambiguous => rejected
  holes.crewId -> crews.crewId             names match, values don't  => rejected (no name-only approval)
  drill_problems.holeId -> holes.holeId    has orphans                => rejected
  comments.holeId -> holes.holeId          clean but tiny sample      => rejected (too few matches)
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import duckdb  # noqa: E402

import config  # noqa: E402
import data_inspector  # noqa: E402
import database as db  # noqa: E402
import recovery  # noqa: E402
import schema_analyzer  # noqa: E402
from ingestion import load_mappings, run_ingestion  # noqa: E402
from llm_service import strip_reasoning  # noqa: E402

FMT = "%Y-%m-%dT%H:%M:%S"
SECRETS = ("Kestrel", "Osprey", "BL-007", "H-00042", "S07", "jammed rods near")


def generate() -> dict[str, list[dict]]:
    rnd = random.Random(11)
    t0 = datetime(2024, 3, 1, 6, 0)
    rigs = ["Kestrel", "Osprey", "Harrier", "Falcon"]
    sites = [f"S{i:02d}" for i in range(25)]
    blasts = [{"blastId": f"BL-{b:03d}", "firedAt": (t0 + timedelta(days=3 * b)).strftime(FMT),
               "explosiveKg": round(rnd.uniform(800, 1200), 1)} for b in range(40)]
    holes = []
    for i in range(800):
        start = t0 + timedelta(hours=2 * i)
        depth = round(rnd.uniform(8, 20), 2)
        minutes = depth * (2 + i % 4) * rnd.uniform(0.9, 1.1)
        holes.append({"holeId": f"H-{i:05d}", "blastId": f"BL-{i % 40:03d}", "rigId": rigs[i % 4],
                      "siteCode": sites[i % 25], "crewId": f"C-{i % 30:03d}",
                      "drillStart": start.strftime(FMT), "drillEnd": (start + timedelta(minutes=minutes)).strftime(FMT),
                      "depthM": depth, "drillTime": round(minutes, 2), "status": rnd.choice(["done", "aborted"])})
    site_rows = [{"siteCode": s, "region": f"R{i % 3}"} for i, s in enumerate(sites)]
    archive_rows = [{"siteCode": s, "archivedYear": 2020 + i % 3} for i, s in enumerate(sites)]
    crews = [{"crewId": f"K-{i:03d}", "crewSize": 3 + i % 4} for i in range(30)]
    problems = [{"problemId": f"P-{i:04d}", "holeId": (holes[rnd.randrange(800)]["holeId"] if i % 15 else f"H-9{i:05d}"),
                 "reportedAt": (t0 + timedelta(hours=7 * i)).strftime(FMT), "category": rnd.choice(["stuck", "water", "wear"])}
                for i in range(150)]
    comments = [{"commentId": f"CM-{i:03d}", "holeId": holes[i * 7]["holeId"],
                 "createdAt": (t0 + timedelta(hours=11 * i)).strftime(FMT),
                 "text": f"synthetic remark {i}: jammed rods near collar, operator noted extended flushing cycle {i * 13}"}
                for i in range(60)]
    return {"blasts": blasts, "holes": holes, "sites": site_rows, "site_archive": archive_rows, "crews": crews,
            "drill_problems": problems, "comments": comments}


class RecoveryBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_recovery_synthetic_"))
        cls._orig_temp = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"
        cls.data_dir, cls.reports = cls.tmp / "data", cls.tmp / "reports"
        cls.data_dir.mkdir()
        cls.data = generate()
        for name, rows in cls.data.items():
            (cls.data_dir / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        cls.db_path = cls.tmp / "blast.duckdb"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert data_inspector.main(["--data-dir", str(cls.data_dir), "--output-dir", str(cls.reports)]) == 0
            assert run_ingestion(load_mappings(cls.tmp / "none.json", cls.data_dir), data_dir=cls.data_dir,
                                 db_path=cls.db_path) == 0
            assert schema_analyzer.main(["--profile", str(cls.reports / "data_profile.json"),
                                         "--output-dir", str(cls.reports), "--force", "--no-interactive"]) == 0
        cls.args = ["--db", str(cls.db_path), "--data-dir", str(cls.data_dir), "--reports-dir", str(cls.reports),
                    "--salt-file", str(cls.tmp / "salt.txt"), "--models-dir", str(cls.tmp / "models")]
        cls.raw_before = cls.fingerprint()
        cls.validate_out = cls.run_cli("validate")
        cls.prepare_out = cls.run_cli("prepare-demo")

    @classmethod
    def tearDownClass(cls) -> None:
        config.DB_TEMP_DIR = cls._orig_temp
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_cli(cls, command: str, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(recovery, "_backend_running", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = recovery.main([command, *cls.args, *extra])
        return rc, out.getvalue()

    @classmethod
    def fingerprint(cls) -> dict:
        with db.db_connection(cls.db_path, read_only=True) as con:
            return {t: con.execute(f"SELECT count(*), sum(hash(payload)) FROM {t}").fetchone()
                    for t in db.list_raw_tables(con)}

    def load(self, name: str) -> dict:
        return json.loads((self.reports / name).read_text(encoding="utf-8"))

    def decision(self, src_ds: str, src_field: str, tgt_ds: str) -> dict:
        report = self.load("recovery_report.json")["validate"]
        return next(d for d in report["decisions"]
                    if d["from"] == {"dataset": src_ds, "field": src_field} and d["to"]["dataset"] == tgt_ds)


class TestValidate(RecoveryBase):
    def test_cli_succeeded(self) -> None:
        self.assertEqual(self.validate_out[0], 0, self.validate_out[1])
        self.assertEqual(self.prepare_out[0], 0, self.prepare_out[1])

    def test_only_the_clean_relationship_is_approved(self) -> None:
        good = self.decision("holes.jsonl", "blastId", "blasts.jsonl")
        self.assertEqual(good["decision"], "approved", good)
        self.assertEqual(good["evidence"]["orphan_records"], 0)
        self.assertEqual(good["evidence"]["observed_cardinality"], "many-to-one")
        approved = [d for d in self.load("recovery_report.json")["validate"]["decisions"] if d["decision"] == "approved"]
        self.assertEqual(len(approved), 1, [(d["from"], d["to"]) for d in approved])

    def test_ambiguous_name_only_orphans_and_tiny_samples_are_rejected(self) -> None:
        for tgt in ("sites.jsonl", "site_archive.jsonl"):
            d = self.decision("holes.jsonl", "siteCode", tgt)
            self.assertEqual((d["decision"], d["reasons"]), ("rejected", ["ambiguous_source_field"]))
        crew = self.decision("holes.jsonl", "crewId", "crews.jsonl")
        self.assertEqual(crew["decision"], "rejected")
        self.assertEqual(crew["evidence"]["matched_values"], 0)
        orphan = self.decision("drill_problems.jsonl", "holeId", "holes.jsonl")
        self.assertIn("orphan_records", orphan["reasons"])
        tiny = self.decision("comments.jsonl", "holeId", "holes.jsonl")
        self.assertIn("too_few_matched_values", tiny["reasons"])

    def test_template_records_auto_decisions_and_pks(self) -> None:
        t = self.load("schema_mapping_template.json")
        for r in t["relationships"]:
            self.assertEqual(r["decided_by"], recovery.AUTO)
            self.assertEqual(r["confirmed"], r["auto_decision"] == "approved")
        self.assertEqual(t["datasets"]["holes.jsonl"]["primary_key"]["field"], "holeId")
        self.assertTrue(t["datasets"]["blasts.jsonl"]["primary_key"]["confirmed"])
        v = self.load("relationship_validation.json")
        self.assertEqual([r["status"] for r in v["relationships"]], ["passed_pending_review"])

    def test_raw_tables_unchanged(self) -> None:
        self.assertEqual(self.fingerprint(), self.raw_before)


class TestPrepareDemo(RecoveryBase):
    def test_standalone_views_and_only_the_approved_join(self) -> None:
        tvs = self.load("typed_views_summary.json")
        self.assertTrue(tvs["views_created"])
        self.assertEqual(len(tvs["dataset_views"]), 7)
        created = [j for j in tvs["join_views"] if j["status"] == "created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["child"]["dataset"], "holes.jsonl")
        self.assertEqual(created[0]["parent"]["dataset"], "blasts.jsonl")
        holes = next(d for d in tvs["dataset_views"] if d["dataset"] == "holes.jsonl")
        self.assertIn("depthM", {c["column"] for c in holes["columns"]})
        comments = next(d for d in tvs["dataset_views"] if d["dataset"] == "comments.jsonl")
        self.assertNotIn("text", {c["column"] for c in comments["columns"]})       # free text never auto-included

    def test_mapping_is_conservative_and_evidence_based(self) -> None:
        m = self.load("analytics_mapping.json")["entities"]
        h = m["holes"]
        self.assertTrue(h["confirmed"])
        self.assertEqual(h["view"], "tv_holes")
        self.assertEqual(h["columns"]["rig"], "rigId")           # drillTime also name-matches 'drill' but fails checks
        self.assertEqual((h["columns"]["start_time"], h["columns"]["end_time"]), ("drillStart", "drillEnd"))
        self.assertIsNone(h["columns"]["duration"])              # computed from the validated start/end pair
        self.assertIsNone(h["columns"]["status"])                # completed values unknown -> not guessed
        self.assertIn("status", h["auto_checks"]["roles_dropped"])
        self.assertTrue(m["blasts"]["confirmed"])
        p = m["problems"]
        self.assertTrue(p["confirmed"])
        self.assertEqual(p["view"], "tv_drill_problems")         # comments are never treated as problems

    def test_analytics_marks_unavailable_features_honestly(self) -> None:
        a = {x["metric"]: x for x in self.load("analytics_summary.json")["availability"]}
        self.assertEqual(a["total_holes"]["status"], "ok")
        self.assertEqual(a["completion_rate"]["status"], "unavailable")
        self.assertEqual(a["drilling_duration"]["status"], "ok")
        self.assertEqual(a["rig_performance"]["status"], "ok")
        self.assertIn(a["prediction_readiness"]["status"], ("ok", "unavailable"))
        summary = self.load("analytics_summary.json")
        holes_metric = next(x for x in summary["overview"] if x["key"] == "total_holes")
        self.assertEqual(holes_metric["value"], 800)

    def test_reports_contain_no_record_values(self) -> None:
        for name in ("recovery_report.json", "recovery_report.md", "analytics_mapping.json"):
            text = (self.reports / name).read_text(encoding="utf-8")
            for s in SECRETS:
                self.assertNotIn(s, text, f"{s!r} leaked into {name}")
        for s in SECRETS:
            self.assertNotIn(s, self.validate_out[1] + self.prepare_out[1], f"{s!r} printed to the console")

    def test_rerun_is_idempotent_and_raw_tables_unchanged(self) -> None:
        before = self.load("schema_mapping_template.json")
        rc, out = self.run_cli("prepare-demo")
        self.assertEqual(rc, 0, out)
        after = self.load("schema_mapping_template.json")
        self.assertEqual([(r["id"], r["confirmed"]) for r in before["relationships"]],
                         [(r["id"], r["confirmed"]) for r in after["relationships"]])
        self.assertEqual(self.fingerprint(), self.raw_before)

    def test_human_decisions_are_never_overwritten(self) -> None:
        t = self.load("schema_mapping_template.json")
        crew = next(r for r in t["relationships"] if r["from"]["field"] == "crewId")
        crew["confirmed"] = True
        crew.pop("decided_by")                                    # a person confirmed it by hand
        (self.reports / "schema_mapping_template.json").write_text(json.dumps(t), encoding="utf-8")
        rc, out = self.run_cli("validate")
        self.assertEqual(rc, 0, out)
        d = self.decision("holes.jsonl", "crewId", "crews.jsonl")
        self.assertEqual(d["decision"], "kept_human")
        t2 = self.load("schema_mapping_template.json")
        self.assertTrue(next(r for r in t2["relationships"] if r["from"]["field"] == "crewId")["confirmed"])
        v = {r["id"]: r for r in self.load("relationship_validation.json")["relationships"]}
        self.assertEqual(v[crew["id"]]["status"], "failed")       # typed_views will still refuse to join it

    def test_column_selection_failure_leaves_template_untouched(self) -> None:
        template_file = self.reports / "schema_mapping_template.json"
        mapping_file = self.reports / "analytics_mapping.json"
        before, mapping_before = template_file.read_bytes(), mapping_file.read_bytes()
        out = io.StringIO()
        with mock.patch.object(recovery, "_backend_running", return_value=False), \
                mock.patch.object(recovery, "select_columns", side_effect=duckdb.OutOfMemoryException("synthetic")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = recovery.main(["prepare-demo", *self.args])
        self.assertEqual(rc, 2)
        self.assertIn("column selection failed (OutOfMemoryException)", out.getvalue())
        self.assertIn("BLAST_MEASURE_BATCH_COLUMNS", out.getvalue())
        self.assertEqual(template_file.read_bytes(), before)          # no partial column approvals
        self.assertEqual(mapping_file.read_bytes(), mapping_before)   # later steps never ran

    def test_prepare_refuses_while_backend_runs(self) -> None:
        out = io.StringIO()
        with mock.patch.object(recovery, "_backend_running", return_value=True), contextlib.redirect_stderr(out):
            rc = recovery.main(["prepare-demo", *self.args])
        self.assertEqual(rc, 2)
        self.assertIn("backend is running", out.getvalue())


class TestCheckAndHelpers(unittest.TestCase):
    def test_check_on_missing_everything(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="blast_recovery_empty_"))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = recovery.main(["check", "--db", str(tmp / "x.duckdb"), "--data-dir", str(tmp),
                                    "--reports-dir", str(tmp)])
            self.assertEqual(rc, 1)
            self.assertIn("Next step:", out.getvalue())
            self.assertIn("[!!] database file", out.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_judge_rejects_everything_but_a_clean_pass(self) -> None:
        base = {"status": "passed_pending_review", "observed_cardinality": "many-to-one", "cardinality_consistent": True,
                "source_side": {"is_array_path": False, "null_key_pct": 0.0},
                "target_side": {"is_unique": True, "rows_with_null_key": 0},
                "matching": {"unmatched_values": 0, "orphan_records": 0, "matched_values": 500, "matched_distinct_keys": 40}}
        self.assertEqual(recovery.judge_relationship(base, recovery.Policy()), [])
        warn = json.loads(json.dumps(base)) | {"status": "warning"}
        self.assertIn("validation_warning", recovery.judge_relationship(warn, recovery.Policy()))
        self.assertEqual(recovery.judge_relationship({"status": "error"}, recovery.Policy()), ["validation_error"])

    def test_strip_reasoning(self) -> None:
        self.assertEqual(strip_reasoning("<think>count 999 rows</think>\nFacts: 40 blasts."), "Facts: 40 blasts.")
        self.assertEqual(strip_reasoning("<think>never finished"), "")
        self.assertEqual(strip_reasoning("Plain answer."), "Plain answer.")


if __name__ == "__main__":
    unittest.main()
