"""
Inventory, hole-summary, capability and evidence questions - SYNTHETIC data only.

The fixture deliberately reproduces the "2,266 holes vs n=288,434" situation:
hole IDs repeat across records (3 records per hole) and are only unique within a blast
(hole "1".."20" in every blast). The assistant must report record rows, non-null IDs,
distinct IDs and distinct (blast, hole) pairs as separate, labelled counts.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_service as cs  # noqa: E402
import config  # noqa: E402
import safe_query as sq  # noqa: E402
import typed_views as tv  # noqa: E402
from analytics_mapping import approved_views, resolve_mapping, suggest_mapping  # noqa: E402
from ingestion import default_table_name, load_mappings, run_ingestion  # noqa: E402
from llm_service import OllamaClient  # noqa: E402
from test_chat import MODEL, FakeOllama  # noqa: E402

FMT = "%Y-%m-%dT%H:%M:%S"
BLASTS, HOLES_PER_BLAST, PASSES = 10, 20, 3
SECRETS = ("Kestrel", "Osprey", "BX-003")


def generate() -> dict[str, list[dict]]:
    t0 = datetime(2024, 1, 3, 6, 0)
    blasts = [{"blastId": f"BX-{b:03d}", "firedAt": (t0 + timedelta(days=9 * b)).strftime(FMT),
               "chargeKg": 900 + 7 * b} for b in range(BLASTS)]
    holes = []
    for b in range(BLASTS):
        for h in range(1, HOLES_PER_BLAST + 1):
            for p in range(PASSES):                          # several records per physical hole
                start = t0 + timedelta(days=9 * b, minutes=40 * (h * PASSES + p))
                minutes = 20 + h + 3 * p
                holes.append({"holeId": str(h), "blastId": f"BX-{b:03d}", "rigId": ["Kestrel", "Osprey"][h % 2],
                              "drillStart": start.strftime(FMT),
                              "drillEnd": (start + timedelta(minutes=minutes)).strftime(FMT),
                              "depthM": round(8 + h * 0.5 + p * 0.1, 2), "status": "done"})
    return {"blasts": blasts, "holes": holes}


def _col(field: str, type_: str) -> dict:
    return {"field": field, "role": "synthetic", "suggested_type": type_, "type": type_, "include": True}


class QuestionBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_questions_synthetic_"))
        cls._orig = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"
        cls.data = generate()
        data_dir = cls.tmp / "data"
        data_dir.mkdir()
        for name, rows in cls.data.items():
            (data_dir / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        db_path = cls.tmp / "q.duckdb"
        assert run_ingestion(load_mappings(cls.tmp / "none.json", data_dir), data_dir=data_dir, db_path=db_path) == 0
        cols = {"blasts.jsonl": [_col("blastId", "VARCHAR"), _col("firedAt", "TIMESTAMP"), _col("chargeKg", "DOUBLE")],
                "holes.jsonl": [_col("holeId", "VARCHAR"), _col("blastId", "VARCHAR"), _col("rigId", "VARCHAR"),
                                _col("drillStart", "TIMESTAMP"), _col("drillEnd", "TIMESTAMP"), _col("depthM", "DOUBLE"),
                                _col("status", "VARCHAR")]}
        template = {"template_version": 1, "relationships": [], "datasets": {
            n: {"entity": n.split(".")[0].upper(), "table": default_table_name(n), "columns": c,
                "primary_key": {"field": None, "confirmed": False}} for n, c in cols.items()}}
        tpath = cls.tmp / "template.json"
        tpath.write_text(json.dumps(template), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            tv.main(["--template", str(tpath), "--validation", str(cls.tmp / "none.json"), "--db", str(db_path),
                     "--output-dir", str(cls.tmp / "tv_out"), "--force"])
        cls.con = sq.open_hardened_connection(db_path)
        cls.views = approved_views(cls.con)
        raw = suggest_mapping(cls.views)
        for name in ("holes", "blasts"):
            raw["entities"][name]["confirmed"] = True
        raw["entities"]["holes"]["columns"]["status"] = None        # completion values unknown -> unavailable
        cls.mapping = resolve_mapping(raw, cls.views)
        cls.fake = FakeOllama()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.con.close()
        cls.fake.close()
        config.DB_TEMP_DIR = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def svc(self, *, llm=None) -> cs.ChatService:
        return cs.ChatService(self.con, self.mapping, llm, salt="0" * 32, approved_views=set(self.views),
                              settings=cs.ChatSettings(use_llm=llm is not None))

    def assert_no_secrets(self, r: dict) -> None:
        text = json.dumps(r)
        for s in SECRETS:
            self.assertNotIn(s, text)


class TestClassification(unittest.TestCase):
    CASES = [
        ("What datasets are available in the drilling database?", "inventory"),
        ("Which tables do you have?", "inventory"),
        ("What data is in the database?", "inventory"),
        ("How many records are in each dataset?", "inventory"),
        ("What time period does the data cover?", "inventory"),
        ("Summarize the available historical hole data. Include relevant metrics and sample sizes.", "hole_summary"),
        ("Give me statistics on the drill holes", "hole_summary"),
        ("Describe the hole data", "hole_summary"),
        ("Which drilling and blasting performance metrics can you reliably calculate, and which are unavailable? "
         "Explain why. What evidence supports your answer? Identify the source datasets and explain any limitations.",
         "capabilities"),
        ("Which metrics are unavailable and why?", "capabilities"),
        ("What can you calculate reliably?", "capabilities"),
        # earlier phrasings keep their meaning
        ("How many holes were drilled?", "overview"),
        ("How many blasts were there?", "overview"),
        ("Compare rig performance and utilisation", "rig_comparison"),
        ("Were there any unusual drilling durations?", "anomalies"),
        ("How many holes per site?", "breakdown"),
        ("How often do drilling problems occur?", "problems"),
        ("What is the average drilling duration?", "drilling_performance"),
        ("Show the monthly drilling trend", "trends"),
        ("Can we predict drilling duration?", "prediction_readiness"),
        ("Give me an overview", "overview"),
    ]

    def test_operations(self) -> None:
        for q, op in self.CASES:
            with self.subTest(q=q):
                intent = cs.rule_intent(q)
                self.assertIsNotNone(intent, q)
                self.assertEqual(intent.operation, op)

    def test_unrelated_questions_stay_unmatched(self) -> None:
        for q in ("Write me a poem about the ocean", "Tell me something interesting about our operations",
                  "What about February 2024?", "Tell me how things developed lately"):
            with self.subTest(q=q):
                self.assertIsNone(cs.rule_intent(q))


class TestInventory(QuestionBase):
    def test_lists_datasets_with_record_counts_and_sources(self) -> None:
        r = self.svc().ask("What datasets are available in the drilling database?")
        self.assertEqual(r["operation"], "inventory")
        by_view = {f["sources"][0]["view"]: f for f in r["facts"] if f["metric"] == "dataset_inventory" and f["sources"]}
        self.assertEqual(by_view["tv_holes"]["value"], len(self.data["holes"]))
        self.assertEqual(by_view["tv_blasts"]["value"], BLASTS)
        self.assertEqual(by_view["tv_holes"]["count_kind"], "total_rows")
        self.assertEqual(by_view["tv_holes"]["sources"][0]["dataset"], "holes.jsonl")
        self.assertIn("used by analytics as 'holes'", by_view["tv_holes"]["statement"])
        cov = next(f for f in r["facts"] if f["count_kind"] == "coverage" and "Hole" in f["statement"])
        self.assertIn("2024-", cov["statement"])
        self.assertTrue(r["limitations"])
        self.assertIn("Facts (from the database):", r["answer"])
        self.assertIn("Limitations:", r["answer"])
        self.assert_no_secrets(r)


class TestHoleSummaryCounts(QuestionBase):
    def test_counts_are_separate_and_labelled(self) -> None:
        r = self.svc().ask("Summarize the available historical hole data. Include relevant metrics and sample sizes.")
        self.assertEqual(r["operation"], "hole_summary")
        rows, ids, pairs = len(self.data["holes"]), HOLES_PER_BLAST, BLASTS * HOLES_PER_BLAST
        main = next(f for f in r["facts"] if f["metric"] == "total_holes" and f["count_kind"] == "distinct"
                    and "pairs" not in f["statement"])
        self.assertEqual((main["value"], main["unit"]), (ids, "distinct hole IDs"))
        self.assertEqual(main["sample_size"], rows)
        self.assertEqual(main["sample_meaning"], "hole records with a non-null hole ID")
        kinds = {f["count_kind"]: f["value"] for f in r["facts"] if f["metric"] == "total_holes"}
        self.assertEqual(kinds["total_rows"], rows)
        self.assertEqual(kinds["non_null"], rows)
        pair_fact = next(f for f in r["facts"] if "pairs" in f["statement"])
        self.assertEqual(pair_fact["value"], pairs)
        # never present the record count as a number of holes
        for f in r["facts"]:
            if f.get("unit") in ("holes", "distinct hole IDs"):
                self.assertNotEqual(f["value"], rows, f["statement"])
        lim = " ".join(x["text"] for x in r["limitations"])
        self.assertIn(f"{rows / ids:.1f} records per ID", lim)
        self.assertIn("(blast ID, hole ID) pairs", lim)
        self.assertIn(f"n={rows:,} hole records with a non-null hole ID", r["answer"])
        self.assertFalse(any("internal count check failed" in w for w in r["warnings"]))

    def test_other_metrics_state_what_n_counts(self) -> None:
        r = self.svc().ask("Describe the hole data")
        dur = next(f for f in r["facts"] if f["metric"] == "drilling_duration")
        self.assertEqual(dur["sample_meaning"], "hole records with a valid duration (> 0)")
        self.assertEqual(dur["sample_size"], len(self.data["holes"]))
        self.assertTrue(any(f["statement"].startswith("Rigs reported:") for f in r["facts"]))
        self.assertTrue(any(u["metric"] == "completion_rate" for u in r["unavailable"]))

    def test_filters_distinguish_total_and_filtered_rows(self) -> None:
        r = self.svc().ask("Summarize the hole data for January 2024")
        kinds = {f["count_kind"]: f["value"] for f in r["facts"] if f["metric"] == "total_holes"}
        expected = sum(1 for h in self.data["holes"] if h["drillStart"].startswith("2024-01"))
        self.assertEqual(kinds["filtered_rows"], expected)
        self.assertEqual(kinds["total_rows"], len(self.data["holes"]))
        self.assertLess(expected, len(self.data["holes"]))

    def test_count_consistency_check(self) -> None:
        self.assertEqual(cs.count_consistency_problems(
            {"total_rows": 600, "filtered_rows": 600, "non_null_ids": 600, "distinct_ids": 20}), [])
        self.assertTrue(cs.count_consistency_problems(
            {"total_rows": 10, "filtered_rows": 12, "non_null_ids": 12, "distinct_ids": 20}))


class TestCapabilities(QuestionBase):
    Q = ("Which drilling and blasting performance metrics can you reliably calculate, and which are unavailable? "
         "Explain why. What evidence supports your answer? Identify the source datasets and explain any limitations.")

    def test_available_unavailable_with_reasons_and_evidence(self) -> None:
        r = self.svc().ask(self.Q)
        self.assertEqual(r["operation"], "capabilities")
        available = [f["statement"] for f in r["facts"] if f["statement"].startswith("Available")]
        self.assertTrue(any("Drilling duration" in s for s in available))
        self.assertTrue(any("[source: holes.jsonl]" in s for s in available))
        unav = {u["metric"]: u["reason"] for u in r["unavailable"]}
        self.assertIn("completion_rate", unav)
        self.assertIn("problem_frequency", unav)
        self.assertTrue(all(reason for reason in unav.values()))
        self.assertTrue(any("records per ID" in x["text"] for x in r["limitations"]))
        self.assertTrue(any("analytics_mapping" == x["source"] for x in r["limitations"]))
        self.assertIn("Unavailable:", r["answer"])
        self.assertIn("tv_holes", {s["view"] for s in r["evidence"]["sources"]})
        self.assert_no_secrets(r)


class TestFormatting(QuestionBase):
    def test_unsupported_answer_is_a_readable_list(self) -> None:
        r = self.svc().ask("Write me a poem about the ocean")
        self.assertEqual(r["operation"], "unsupported")
        self.assertTrue(r["answer"].startswith("I can't answer that question from the database yet."))
        self.assertIn("\n- available datasets, record counts and data coverage", r["answer"])
        self.assertEqual(r["limitations"], [])

    def test_llm_raw_json_answer_is_rejected(self) -> None:
        self.fake.responder = lambda p: '{"answer": "20 distinct hole IDs"}' if not p.get("format") else '{"operation": "unsupported"}'
        r = self.svc(llm=OllamaClient(self.fake.url, MODEL, timeout=5)).ask("Describe the hole data")
        self.assertEqual(r["answer_source"], "template")
        self.assertTrue(any("raw JSON" in w for w in r["warnings"]))
        self.assertNotIn('{"answer"', r["answer"])

    def test_llm_answer_with_reasoning_block_is_grounded(self) -> None:
        self.fake.responder = (lambda p: "<think>maybe 999 holes?</think>Facts: 20 distinct hole IDs (n=600)."
                               if not p.get("format") else '{"operation": "unsupported"}')
        r = self.svc(llm=OllamaClient(self.fake.url, MODEL, timeout=5)).ask("Describe the hole data")
        self.assertEqual(r["answer_source"], "llm")
        self.assertNotIn("<think>", r["answer"])
        self.assertNotIn("999", r["answer"])


if __name__ == "__main__":
    unittest.main()
