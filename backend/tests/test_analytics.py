"""
End-to-end tests for analytics.py / anomaly_detection.py / predictions.py using SYNTHETIC data only.

Pipeline under test (all inside a temp directory):
    synthetic JSONL -> ingestion -> typed views -> analytics mapping -> analytics

Every expected number is computed independently in Python from the generated
synthetic records. No company data, data/, local_db/ or local_reports/ is used.

Run:  python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import json
import random
import shutil
import statistics
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import analytics as an  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402
import typed_views as tv  # noqa: E402
from analytics_mapping import approved_views, resolve_mapping, suggest_mapping  # noqa: E402
from ingestion import default_table_name, load_mappings, run_ingestion  # noqa: E402

RIG_FACTORS = {"Kestrel": 2.0, "Osprey": 3.0, "Harrier": 2.5, "Falcon": 4.0}
SITES = ["Basalt Ridge", "Copper Flat", "Dune Pit"]
SECRETS = ["Kestrel", "Osprey", "Merlin", "Basalt Ridge", "BL-001", "H-00010", "stuck bit"]
FMT = "%Y-%m-%dT%H:%M:%S"


def generate() -> dict[str, list[dict]]:
    rnd = random.Random(7)
    blasts, holes, problems = [], [], []
    t0 = datetime(2024, 1, 1, 6, 0, 0)
    hole_no = 0
    rigs = list(RIG_FACTORS)
    for b in range(40):
        fired = t0 + timedelta(hours=108 * b)
        site = SITES[b % 3]
        kg = round(rnd.uniform(800, 1200), 1) if b != 17 else 25000.0      # blast 17: anomalous explosive mass
        blasts.append({"blastId": f"BL-{b:03d}", "firedAt": fired.strftime(FMT), "siteName": site,
                       "explosiveKg": kg, "powderFactor": round(rnd.uniform(0.4, 0.9), 3)})
        cursor = {r: fired - timedelta(hours=60) for r in rigs}
        for i in range(rnd.randint(20, 40)):
            rig = rigs[i % 4]
            depth = round(rnd.uniform(8, 20), 2)
            minutes = depth * RIG_FACTORS[rig] * rnd.uniform(0.9, 1.1)
            start = cursor[rig]
            end = start + timedelta(minutes=minutes)
            cursor[rig] = end + timedelta(minutes=20)
            holes.append({"holeId": f"H-{hole_no:05d}", "blastId": f"BL-{b:03d}", "rigId": rig, "siteName": site,
                          "drillStart": start.strftime(FMT), "drillEnd": end.strftime(FMT), "depthM": depth,
                          "status": rnd.choices(["done", "aborted", None], [88, 10, 2])[0]})
            hole_no += 1
    for idx in (50, 300, 600, 900, 1000):                       # injected long-duration anomalies
        h = holes[idx]
        s = datetime.strptime(h["drillStart"], FMT)
        h["drillEnd"] = (s + timedelta(minutes=900)).strftime(FMT)
    s = datetime.strptime(holes[123]["drillStart"], FMT)        # injected invalid interval
    holes[123]["drillEnd"] = (s - timedelta(minutes=5)).strftime(FMT)
    for i in range(3):                                          # tiny rig -> must be suppressed
        holes.append({"holeId": f"H-M{i}", "blastId": "BL-000", "rigId": "Merlin", "siteName": SITES[0],
                      "drillStart": (t0 + timedelta(hours=i)).strftime(FMT),
                      "drillEnd": (t0 + timedelta(hours=i, minutes=30)).strftime(FMT), "depthM": 10.0,
                      "status": "done"})
    for e in range(150):
        h = holes[rnd.randrange(len(holes) - 3)]
        cat = "collapse" if e < 2 else rnd.choice(["stuck bit", "water ingress", "bit wear"])
        problems.append({"eventId": f"EV-{e:04d}", "eventTime": h["drillStart"], "rigId": h["rigId"],
                         "holeId": h["holeId"], "blastId": h["blastId"], "category": cat})
    return {"blasts": blasts, "holes": holes, "problem_events": problems}


def duration_minutes(h: dict) -> float | None:
    if not h.get("drillStart") or not h.get("drillEnd"):
        return None
    return (datetime.strptime(h["drillEnd"], FMT) - datetime.strptime(h["drillStart"], FMT)).total_seconds() / 60


def _col(field: str, type_: str) -> dict:
    return {"field": field, "role": "synthetic", "suggested_type": type_, "type": type_, "include": True}


class AnalyticsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_an_synthetic_"))
        cls._orig = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"
        cls.data = generate()
        data_dir = cls.tmp / "data"
        data_dir.mkdir()
        for name, rows in cls.data.items():
            (data_dir / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        cls.db_path = cls.tmp / "synthetic.duckdb"
        assert run_ingestion(load_mappings(cls.tmp / "none.json", data_dir), data_dir=data_dir, db_path=cls.db_path) == 0

        cols = {
            "blasts.jsonl": [_col("blastId", "VARCHAR"), _col("firedAt", "TIMESTAMP"), _col("siteName", "VARCHAR"),
                             _col("explosiveKg", "DOUBLE"), _col("powderFactor", "DOUBLE")],
            "holes.jsonl": [_col("holeId", "VARCHAR"), _col("blastId", "VARCHAR"), _col("rigId", "VARCHAR"),
                            _col("siteName", "VARCHAR"), _col("drillStart", "TIMESTAMP"),
                            _col("drillEnd", "TIMESTAMP"), _col("depthM", "DOUBLE"), _col("status", "VARCHAR")],
            "problem_events.jsonl": [_col("eventId", "VARCHAR"), _col("eventTime", "TIMESTAMP"),
                                     _col("rigId", "VARCHAR"), _col("holeId", "VARCHAR"), _col("blastId", "VARCHAR"),
                                     _col("category", "VARCHAR")],
        }
        template = {"template_version": 1, "relationships": [], "datasets": {
            n: {"entity": n.split(".")[0].upper(), "table": default_table_name(n), "columns": c,
                "primary_key": {"field": None, "confirmed": False}} for n, c in cols.items()}}
        tpath = cls.tmp / "template.json"
        tpath.write_text(json.dumps(template), encoding="utf-8")
        tv.main(["--template", str(tpath), "--validation", str(cls.tmp / "none.json"), "--db", str(cls.db_path),
                 "--output-dir", str(cls.tmp / "tv_out"), "--force"])

        with db.db_connection(cls.db_path, read_only=True) as con:
            cls.views = approved_views(con)
        cls.suggestion = suggest_mapping(cls.views)
        cls.mapping_raw = json.loads(json.dumps(cls.suggestion))
        for e in cls.mapping_raw["entities"].values():
            e["confirmed"] = True
        cls.mapping_raw["entities"]["holes"]["completed_values"] = ["done"]
        cls.mapping_path = cls.tmp / "analytics_mapping.json"
        cls.mapping_path.write_text(json.dumps(cls.mapping_raw), encoding="utf-8")
        cls.salt = "0123456789abcdef0123456789abcdef"

    @classmethod
    def tearDownClass(cls) -> None:
        config.DB_TEMP_DIR = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def engine_call(self, fn, *, reveal: bool = False, mapping: dict | None = None):
        with db.db_connection(self.db_path, read_only=True) as con:
            m = resolve_mapping(mapping or self.mapping_raw, approved_views(con))
            eng = an.AnalyticsEngine(con, m, salt=self.salt, reveal_labels=reveal)
            return fn(eng)

    @property
    def holes(self) -> list[dict]:
        return self.data["holes"]


class TestMappingSuggestion(AnalyticsTestBase):
    def test_suggestion_finds_views_and_roles(self) -> None:
        e = self.suggestion["entities"]
        self.assertEqual(e["blasts"]["view"], "tv_blasts")
        self.assertEqual(e["holes"]["view"], "tv_holes")
        self.assertEqual(e["problems"]["view"], "tv_problem_events")
        self.assertEqual(e["holes"]["columns"]["rig"], "rigId")
        self.assertEqual(e["holes"]["columns"]["start_time"], "drillStart")
        self.assertEqual(e["holes"]["columns"]["end_time"], "drillEnd")
        self.assertEqual(e["holes"]["columns"]["depth"], "depthM")
        self.assertEqual(e["blasts"]["columns"]["time"], "firedAt")
        self.assertEqual(e["problems"]["columns"]["category"], "category")
        self.assertEqual(sorted(e["blasts"]["measures"]), ["explosiveKg", "powderFactor"])
        self.assertTrue(all(not x["confirmed"] for x in e.values()))

    def test_unconfirmed_mapping_is_not_used(self) -> None:
        path = self.tmp / "unconfirmed.json"
        path.write_text(json.dumps(self.suggestion), encoding="utf-8")
        rc = an.main(["run", "--db", str(self.db_path), "--mapping", str(path), "--force",
                      "--salt-file", str(self.tmp / "salt.txt"), "--output-dir", str(self.tmp / "o0")])
        self.assertEqual(rc, 2)

    def test_bad_role_type_is_rejected(self) -> None:
        bad = json.loads(json.dumps(self.mapping_raw))
        bad["entities"]["holes"]["columns"]["depth"] = "rigId"       # VARCHAR for a numeric role
        with db.db_connection(self.db_path, read_only=True) as con:
            m = resolve_mapping(bad, approved_views(con))
        self.assertNotIn("depth", m.get("holes").columns)
        self.assertTrue(any("holes.depth" in i for i in m.issues))


class TestMetrics(AnalyticsTestBase):
    def test_overview_matches_synthetic_truth(self) -> None:
        ov = {m["key"]: m for m in self.engine_call(lambda e: e.overview(an.Filters()))}
        self.assertEqual(ov["total_blasts"]["value"], 40)
        self.assertEqual(ov["total_holes"]["value"], len(self.holes))
        known = [h for h in self.holes if h["status"] is not None]
        done = [h for h in known if h["status"] == "done"]
        self.assertAlmostEqual(ov["completion_rate"]["value"], round(100 * len(done) / len(known), 2))
        self.assertEqual(ov["completion_rate"]["sample_size"], len(known))
        self.assertEqual(ov["completion_rate"]["missing"]["holes_without_status"], len(self.holes) - len(known))
        n_prob = len(self.data["problem_events"])
        self.assertAlmostEqual(ov["problem_frequency"]["value"], round(100 * n_prob / len(self.holes), 2))
        cats = ov["problem_frequency"]["details"]
        self.assertEqual(cats["categories_suppressed"], 1)                 # "collapse" has 2 records
        for m in ov.values():
            self.assertTrue(m["method"])
            self.assertTrue(m["sources"])

    def test_duration_and_depth(self) -> None:
        perf = {m["key"]: m for m in self.engine_call(lambda e: e.drilling_performance(an.Filters()))}
        durs = [d for d in (duration_minutes(h) for h in self.holes) if d is not None and d > 0]
        d = perf["drilling_duration"]
        self.assertEqual(d["sample_size"], len(durs))
        self.assertEqual(d["missing"]["non_positive_durations_excluded"], 1)
        self.assertAlmostEqual(d["details"]["mean_minutes"], round(statistics.mean(durs), 2), places=2)
        self.assertAlmostEqual(d["details"]["median_minutes"], round(statistics.median(durs), 2), places=2)
        depth = perf["drilled_depth"]
        self.assertAlmostEqual(depth["value"], round(sum(h["depthM"] for h in self.holes), 2), places=2)
        self.assertEqual(perf["drilling_rate"]["status"], "ok")

    def test_filters(self) -> None:
        f_rig = an.Filters(rigs=("Kestrel",))
        holes = self.engine_call(lambda e: e.total_holes(f_rig))
        self.assertEqual(holes["value"], sum(1 for h in self.holes if h["rigId"] == "Kestrel"))
        blasts = self.engine_call(lambda e: e.total_blasts(f_rig))
        self.assertEqual(blasts["status"], "unavailable")                  # blasts have no rig column
        self.assertIn("rig filter", blasts["reason"])
        f_date = an.Filters(date_from=date(2024, 2, 1), date_to=date(2024, 2, 29))
        expected = sum(1 for h in self.holes if "2024-02-01" <= h["drillStart"][:10] <= "2024-02-29")
        self.assertEqual(self.engine_call(lambda e: e.total_holes(f_date))["value"], expected)
        f_blast = an.Filters(blasts=("BL-001", "BL-002"))
        self.assertEqual(self.engine_call(lambda e: e.total_blasts(f_blast))["value"], 2)
        self.assertEqual(self.engine_call(lambda e: e.total_holes(f_blast))["value"],
                         sum(1 for h in self.holes if h["blastId"] in ("BL-001", "BL-002")))

    def test_rig_performance_pseudonymised_and_suppressed(self) -> None:
        rp = self.engine_call(lambda e: e.rig_performance(an.Filters()))
        rigs = rp["details"]["rigs"]
        self.assertEqual(len(rigs), 4)
        self.assertEqual(rp["details"]["rigs_suppressed_small_sample"], 1)   # Merlin (3 holes)
        self.assertTrue(all(r["group"].startswith("RIG-") for r in rigs))
        for r in rigs:
            self.assertIsNotNone(r["utilization_pct"])
            self.assertTrue(0 < r["utilization_pct"] <= 100)
            self.assertIsNotNone(r["problems_per_100_holes"])
        revealed = self.engine_call(lambda e: e.rig_performance(an.Filters()), reveal=True)
        by_name = {r["group"]: r for r in revealed["details"]["rigs"]}
        self.assertEqual(by_name["Kestrel"]["holes"], sum(1 for h in self.holes if h["rigId"] == "Kestrel"))
        slow, fast = by_name["Falcon"]["median_duration_minutes"], by_name["Kestrel"]["median_duration_minutes"]
        self.assertGreater(slow, fast)                                     # synthetic rig factors 4.0 vs 2.0

    def test_completion_time_and_trends(self) -> None:
        ct = self.engine_call(lambda e: e.completion_time_analysis(an.Filters()))
        self.assertEqual(ct["missing"]["invalid_intervals"], 1)
        shown = sum(b["holes"] or 0 for b in ct["details"]["distribution"])
        self.assertLessEqual(shown, ct["sample_size"])
        tr = self.engine_call(lambda e: e.trends(an.Filters()))
        self.assertIn("holes", tr["details"])
        self.assertIn("blasts", tr["details"])
        periods = [p["period"] for p in tr["details"]["holes"]]
        self.assertTrue(all(len(p) == 7 and p[4] == "-" for p in periods))     # YYYY-MM
        self.assertEqual(periods, sorted(periods))
        self.assertEqual(periods[0], "2023-12")      # first holes start 60 h before the first blast

    def test_blast_analysis_and_comparison(self) -> None:
        ba = self.engine_call(lambda e: e.blast_analysis(an.Filters()))
        self.assertEqual(ba["value"], 40)
        self.assertIn("holes", ba["details"]["per_blast_distribution"])
        cmp_ = self.engine_call(lambda e: e.compare_blasts(["BL-001", "BL-017", "NOPE"], an.Filters()))
        self.assertEqual(cmp_["value"], 2)
        self.assertEqual(cmp_["details"]["not_found"], 1)
        b17 = cmp_["details"]["blasts"][1]
        self.assertEqual(b17["percentile_vs_all_blasts"]["m_explosiveKg"], 100.0)
        self.assertNotIn("m_explosiveKg", b17["values"])                    # raw value hidden without reveal
        self.assertTrue(b17["blast"].startswith("BLAST-"))


class TestAnomaliesAndPrediction(AnalyticsTestBase):
    def test_anomalies(self) -> None:
        res = self.engine_call(lambda e: an.anomaly_detection.detect_all(e, an.Filters()))
        det = {d["detector"]: d for d in res["detectors"]}
        lines = {fl["record"]["_source_line"] for fl in det["hole_duration"]["flagged"]}
        for idx in (50, 300, 600, 900, 1000):
            self.assertIn(idx + 1, lines, f"injected anomaly at line {idx + 1} not flagged")
        self.assertEqual(det["invalid_intervals"]["flagged_count"], 1)
        self.assertEqual(det["invalid_intervals"]["flagged"][0]["record"]["_source_line"], 124)
        self.assertGreaterEqual(det["blast_explosiveKg"]["flagged_count"], 1)
        self.assertEqual(det["blast_explosiveKg"]["flagged"][0]["record"]["_source_line"], 18)
        self.assertEqual(det["isolation_forest"]["status"], "ok")
        self.assertGreater(det["isolation_forest"]["flagged_count"], 0)
        for d in det.values():
            for fl in d["flagged"]:
                self.assertIn("NOT a confirmed operational problem", fl["label"])
                self.assertTrue(fl["explanation"])

    def test_prediction_ready_and_trains(self) -> None:
        models = self.tmp / "models"
        def go(e):
            r = an.predictions.assess_readiness(e, an.Filters())
            return r, an.predictions.train_baseline(e, an.Filters(), r, models)
        readiness, model = self.engine_call(go)
        self.assertTrue(readiness["ready"], readiness["reasons"])
        excluded = {x["role"] for x in readiness["excluded"]}
        self.assertTrue({"end_time", "status"} <= excluded)
        self.assertIn("time-based", readiness["split_strategy"])
        self.assertTrue(model["trained"])
        m = model["metrics"]
        # duration = depth x rig factor (+-10%): a sound model must clearly beat the median baseline
        self.assertGreaterEqual(model["mae_improvement_vs_baseline_pct"], 50.0)
        self.assertIn("beats the median baseline", model["verdict"])
        self.assertTrue((models / "drilling_duration_baseline.joblib").exists())

    def test_prediction_not_ready_on_small_sample(self) -> None:
        f = an.Filters(date_from=date(2024, 1, 1), date_to=date(2024, 1, 3))
        def go(e):
            r = an.predictions.assess_readiness(e, f)
            return r, an.predictions.train_baseline(e, f, r, self.tmp / "models_small")
        readiness, model = self.engine_call(go)
        self.assertFalse(readiness["ready"])
        self.assertTrue(any("valid duration targets" in x for x in readiness["reasons"]))
        self.assertFalse(model["trained"])


class TestCli(AnalyticsTestBase):
    def test_full_run_writes_reports_without_values(self) -> None:
        out = self.tmp / "out"
        rc = an.main(["run", "--db", str(self.db_path), "--mapping", str(self.mapping_path), "--force",
                      "--salt-file", str(self.tmp / "salt.txt"), "--output-dir", str(out),
                      "--compare-blast", "BL-001", "--train", "--models-dir", str(self.tmp / "m_cli")])
        self.assertEqual(rc, 0)
        js = (out / an.JSON_REPORT).read_text(encoding="utf-8")
        md = (out / an.MD_REPORT).read_text(encoding="utf-8")
        report = json.loads(js)
        self.assertTrue(report["availability"])
        self.assertEqual(report["labels"], "pseudonymised")
        for secret in SECRETS:
            self.assertNotIn(secret, js, f"synthetic value {secret!r} leaked into JSON report")
            self.assertNotIn(secret, md, f"synthetic value {secret!r} leaked into Markdown report")

    def test_suggest_mapping_never_overwrites(self) -> None:
        target = self.tmp / "existing_mapping.json"
        target.write_text('{"keep": true}', encoding="utf-8")
        rc = an.main(["suggest-mapping", "--db", str(self.db_path), "--mapping", str(target)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
        self.assertTrue((self.tmp / "existing_mapping.suggested.json").exists())

    def test_missing_inputs(self) -> None:
        self.assertEqual(an.main(["run", "--db", str(self.tmp / "absent.duckdb")]), 2)
        self.assertEqual(an.main(["run", "--db", str(self.db_path), "--mapping", str(self.tmp / "absent.json"),
                                  "--salt-file", str(self.tmp / "s.txt")]), 2)


if __name__ == "__main__":
    unittest.main()
