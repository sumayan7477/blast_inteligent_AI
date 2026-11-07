"""
Bounded-memory conversion checks (typed_views.measure_dataset / recovery.select_columns) - SYNTHETIC data only.

A wide raw table is generated inside a temporary DuckDB file with SQL (no JSONL, no
company data). Tests check that the batched single-parse measurement:
  * returns exactly the same counts as straightforward per-column reference queries,
  * completes under a tight DuckDB memory limit,
  * retries smaller batches on out-of-memory errors without changing results,
  * never treats a column whose check failed as passing (recovery excludes it, with a reason).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import duckdb  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
import recovery  # noqa: E402
import typed_views as tv  # noqa: E402

TYPES = ["BIGINT", "DOUBLE", "TIMESTAMP", "BOOLEAN", "VARCHAR", "INTEGER"]


def _value_sql(j: int) -> str:
    """Synthetic value generator per column type, including bad, empty and null values."""
    kind = j % 6
    if kind in (0, 5):
        return (f"CASE WHEN i % 13 = 0 THEN NULL WHEN i % 97 = 0 THEN 'bad' WHEN i % 89 = 0 THEN '3.5' "
                f"ELSE CAST(i * {j + 1} AS VARCHAR) END")
    if kind == 1:
        return f"CASE WHEN i % 101 = 0 THEN 'NaN' WHEN i % 17 = 0 THEN '' ELSE CAST(i / {j + 3}.0 AS VARCHAR) END"
    if kind == 2:
        return ("CASE WHEN i % 53 = 0 THEN 'not-a-date' ELSE strftime(TIMESTAMP '2024-01-01' + "
                "INTERVAL (i) MINUTE, '%Y-%m-%dT%H:%M:%S') END")
    if kind == 3:
        return "CASE WHEN i % 7 = 0 THEN 'maybe' WHEN i % 2 = 0 THEN 'true' ELSE 'false' END"
    return f"CASE WHEN i % 11 = 0 THEN '' ELSE 'txt' || CAST(i % {j + 5} AS VARCHAR) END"


def build_wide_table(path: Path, rows: int, cols: int) -> None:
    obj = ", ".join(f"'f{j}', {_value_sql(j)}" for j in range(cols))
    with db.db_connection(path) as con:
        db.create_raw_table(con, "raw_wide")
        con.execute(f"""INSERT INTO raw_wide
            SELECT md5(CAST(i AS VARCHAR)), 'wide.jsonl', i + 1, NULL, 'synthetic', TIMESTAMP '2024-01-01',
                   CAST(json_object({obj}) AS JSON)
            FROM range({rows}) t(i)""")


def make_plan(cols: int) -> tv.DatasetPlan:
    plan = tv.DatasetPlan(dataset="wide.jsonl", table="raw_wide", view="tv_wide")
    for j in range(cols):
        plan.columns.append(tv.ColumnPlan(name=f"f{j}", field=f"f{j}", type=TYPES[j % 6],
                                          json_path=f'$."f{j}"', origin="included"))
    return plan


def reference_counts(con, cols: int) -> dict[str, tuple[int, int]]:
    """Original per-column semantics, one simple query per column."""
    out = {}
    for c in make_plan(cols).columns:
        r = tv.raw_text_expr(c.json_path)
        fail = "0" if c.type in tv.NO_FAILURE_TYPES else f"count(*) FILTER (WHERE {r} IS NOT NULL AND {c.expr} IS NULL)"
        out[c.field] = tuple(int(x) for x in con.execute(
            f"SELECT count(*) FILTER (WHERE {r} IS NOT NULL), {fail} FROM raw_wide").fetchone())
    return out


class FlakyCon:
    """Proxy that raises OutOfMemoryException for measurement queries that are too wide or touch a given path."""

    def __init__(self, con, *, max_paths: int | None = None, poison: str | None = None) -> None:
        self.con, self.max_paths, self.poison, self.queries = con, max_paths, poison, 0

    def execute(self, sql: str, *args):
        if "json_extract_string(payload, [" in sql:
            self.queries += 1
            n = sql.count("'$.")
            if (self.max_paths and n > self.max_paths) or (self.poison and f'"{self.poison}"' in sql):
                raise duckdb.OutOfMemoryException("synthetic out-of-memory")
        return self.con.execute(sql, *args)


class TestBatchedMeasurement(unittest.TestCase):
    ROWS, COLS = 20_000, 30

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="blast_measure_synthetic_"))
        cls._orig = config.DB_TEMP_DIR
        config.DB_TEMP_DIR = cls.tmp / "duck_tmp"
        cls.db_path = cls.tmp / "wide.duckdb"
        build_wide_table(cls.db_path, cls.ROWS, cls.COLS)
        with db.db_connection(cls.db_path, read_only=True) as con:
            cls.expected = reference_counts(con, cls.COLS)

    @classmethod
    def tearDownClass(cls) -> None:
        config.DB_TEMP_DIR = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def measured(self, plan: tv.DatasetPlan) -> dict[str, tuple[int, int]]:
        return {c.field: (c.stats["present"], c.stats["conversion_failures"]) for c in plan.columns if "present" in c.stats}

    def test_same_counts_as_reference_for_any_batch_size(self) -> None:
        self.assertTrue(any(f for _, f in self.expected.values()), "fixture must contain conversion failures")
        with db.db_connection(self.db_path, read_only=True) as con:
            for size in (1, 7, 16, 64):
                with self.subTest(batch_size=size):
                    plan = make_plan(self.COLS)
                    tv.measure_dataset(con, plan, warn_pct=1.0, batch_size=size)
                    self.assertEqual(plan.table_rows, self.ROWS)
                    self.assertEqual(self.measured(plan), self.expected)

    def test_rounding_and_nan_are_still_failures(self) -> None:
        with db.db_connection(self.db_path, read_only=True) as con:
            plan = make_plan(self.COLS)
            tv.measure_dataset(con, plan, warn_pct=1.0)
        f0 = plan.column_for("f0")                       # BIGINT with 'bad' and '3.5'
        self.assertGreater(f0.stats["conversion_failures"], 0)
        f1 = plan.column_for("f1")                       # DOUBLE with 'NaN'
        self.assertGreater(f1.stats["conversion_failures"], 0)
        f4 = plan.column_for("f4")                       # VARCHAR never fails
        self.assertEqual(f4.stats["conversion_failures"], 0)

    def test_retries_smaller_batches_on_out_of_memory(self) -> None:
        with db.db_connection(self.db_path, read_only=True) as con:
            flaky = FlakyCon(con, max_paths=4)
            plan = make_plan(self.COLS)
            tv.measure_dataset(flaky, plan, warn_pct=1.0, batch_size=16)
        self.assertEqual(self.measured(plan), self.expected)
        self.assertFalse([c for c in plan.columns if c.status == "error"])
        self.assertGreater(flaky.queries, self.COLS / 16)   # had to split

    def test_column_whose_check_fails_is_never_passed(self) -> None:
        with db.db_connection(self.db_path, read_only=True) as con:
            plan = make_plan(self.COLS)
            tv.measure_dataset(FlakyCon(con, poison="f3"), plan, warn_pct=1.0, batch_size=8)
        bad = plan.column_for("f3")
        self.assertEqual(bad.status, "error")
        self.assertTrue(any("could not complete (OutOfMemoryException)" in m for m in bad.messages))
        others = {k: v for k, v in self.expected.items() if k != "f3"}
        self.assertEqual(self.measured(plan), others)

    def test_recovery_excludes_columns_whose_check_failed(self) -> None:
        template = {"datasets": {"wide.jsonl": {
            "table": "raw_wide", "primary_key": {"field": None, "confirmed": False},
            "columns": [{"field": f"f{j}", "role": "string", "suggested_type": TYPES[j % 6], "type": TYPES[j % 6],
                         "include": False} for j in range(self.COLS)]}}, "relationships": []}
        with db.db_connection(self.db_path, read_only=True) as con:
            summary = recovery.select_columns(FlakyCon(con, poison="f3"), template, recovery.Policy())
        cols = {c["field"]: c for c in template["datasets"]["wide.jsonl"]["columns"]}
        self.assertFalse(cols["f3"]["include"])
        self.assertIn("could not complete", cols["f3"]["auto_reason"])
        self.assertEqual(summary["wide.jsonl"]["excluded_check_failed"], 1)
        self.assertTrue(cols["f4"]["include"])
        self.assertEqual(cols["f0"]["type"], "VARCHAR")   # >1 % BIGINT failures -> kept as text, not approved as BIGINT


class TestTightMemoryLimit(unittest.TestCase):
    """A wide table measured with DuckDB capped at 200 MB and 2 threads must complete."""

    def test_completes_under_memory_limit(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="blast_measure_mem_"))
        orig = (config.DB_TEMP_DIR, config.DUCKDB_MEMORY_LIMIT, config.DUCKDB_THREADS)
        try:
            config.DB_TEMP_DIR = tmp / "duck_tmp"
            path = tmp / "wide.duckdb"
            build_wide_table(path, rows=80_000, cols=64)
            config.DUCKDB_MEMORY_LIMIT, config.DUCKDB_THREADS = "200MB", 2
            with db.db_connection(path, read_only=True) as con:
                plan = make_plan(64)
                tv.measure_dataset(con, plan, warn_pct=1.0)
            self.assertEqual(plan.table_rows, 80_000)
            self.assertFalse([c.field for c in plan.columns if c.status == "error" and "could not complete" in " ".join(c.messages)])
        finally:
            config.DB_TEMP_DIR, config.DUCKDB_MEMORY_LIMIT, config.DUCKDB_THREADS = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestViewSqlUnchanged(unittest.TestCase):
    def test_typed_expr_is_the_same_conversion(self) -> None:
        for t in TYPES + ["DATE", "TIMESTAMPTZ", "EPOCH_S", "EPOCH_MS", "JSON"]:
            path = '$."x"'
            expected = tv.typed_from_text(t, tv.raw_text_expr(path), None,
                                          json_expr=f"json_extract(payload, {tv._sql_str(path)})")
            self.assertEqual(tv.typed_expr(t, path), expected)
        with self.assertRaises(ValueError):
            tv.typed_from_text("JSON", "x")


if __name__ == "__main__":
    unittest.main()
