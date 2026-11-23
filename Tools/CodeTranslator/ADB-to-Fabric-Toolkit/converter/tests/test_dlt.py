import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, validate_config
from adb2fabric.converter import convert_text, build_notebook, _syntax_check
from adb2fabric.model import NotebookResult
from adb2fabric.report import totals

HEADER = "# Databricks notebook source\n"
BATCH = '''import dlt
my_rules = {"valid_product_id": "product_id IS NOT NULL", "valid_price": "price >= 0"}
@dlt.table()
@dlt.expect_all_or_drop(my_rules)
def gold_products():
    return spark.sql("""
SELECT product_id, product_name, price
FROM LIVE.silver_products
""")
'''


class BatchDLT(unittest.TestCase):
    def convert(self, body=BATCH, settings=None, standalone=True):
        config = {
            "enabled": True, "replace_existing": False,
            "target_mappings": {"gold_products": "gold.gold_products"},
            "source_mappings": {"LIVE.silver_products": "silver.silver_products"},
        }
        config.update(settings or {})
        with patch.dict(CONFIG, dlt=config):
            return convert_text(HEADER + body, "products.py", "python", standalone=standalone)

    def sql(self, result):
        calls = []
        code = "\n".join("\n".join(lines) for _, kind, lines in result[0] if kind == "code")
        ast.parse(code)
        exec(code, {"spark": SimpleNamespace(sql=calls.append)})
        return calls

    def test_batch_sql_mlv_static_constraints_and_fallback_removed(self):
        result = self.convert()
        cells, items, ctx, cl, ml, warnings = result
        sql, = self.sql(result)
        self.assertIn("CREATE MATERIALIZED LAKE VIEW gold.gold_products", sql)
        self.assertNotIn("OR REPLACE", sql)
        self.assertIn("CONSTRAINT valid_product_id CHECK (product_id IS NOT NULL) ON MISMATCH DROP", sql)
        self.assertIn("CONSTRAINT valid_price CHECK (price >= 0) ON MISMATCH DROP", sql)
        self.assertIn("FROM silver.silver_products", sql)
        code = "\n".join("\n".join(lines) for _, kind, lines in cells)
        self.assertNotIn("import dlt", code)
        self.assertNotIn("@dlt", code)
        self.assertNotIn("fmlv", code)
        self.assertNotIn("TODO", code)
        self.assertEqual([(i.rule_id, i.level) for i in items],
                         [("X3-IMPORT", "AUTO"), ("X3-TABLE", "REVIEW"), ("X3-EXPECT", "AUTO")])
        self.assertEqual([i.line for i in items], [2, 4, 5])
        self.assertEqual(ml, 0)
        self.assertEqual(warnings, [])
        self.assertIn("dlt", ctx.checklist)
        self.assertEqual(_syntax_check([(i, (k, lines)) for i, k, lines in cells]), [])
        nb = build_notebook("products.py", cells, ctx, items, "python")
        self.assertEqual(len({c["id"] for c in nb["cells"]}), len(nb["cells"]))
        r = NotebookResult("products.py", "products.ipynb", "python", len(cells), cl, ml, items, {}, [], [])
        self.assertEqual(r.status, "Converted - review")
        self.assertAlmostEqual(totals([r])["weighted_coverage"], 2.5 / 3)

    def test_views_are_inlined_with_no_session_scoped_temp_views(self):
        source = '''import dlt
@dlt.table
def gold_products():
    return spark.sql("SELECT * FROM LIVE.active_products")
# COMMAND ----------
@dlt.view()
def active_products():
    return spark.sql("SELECT * FROM LIVE.product_staging WHERE is_active = true")
# COMMAND ----------
@dlt.view
def product_staging():
    return spark.sql("SELECT * FROM silver.products")
'''
        result = self.convert(source)
        sql, = self.sql(result)
        self.assertIn("WITH _a2f_view_2 AS", sql)
        self.assertLess(sql.index("_a2f_view_2 AS"), sql.index("_a2f_view_1 AS"))
        self.assertIn("FROM _a2f_view_2 WHERE is_active = true", sql)
        self.assertIn("SELECT * FROM _a2f_view_1", sql)
        self.assertNotIn("LIVE.", sql)
        self.assertNotIn("createOrReplaceTempView", str(result[0]))
        self.assertEqual(sum(i.rule_id == "X3-VIEW" and i.level == "REVIEW" for i in result[1]), 2)

    def test_table_dependencies_create_in_topological_order(self):
        source = '''import dlt
@dlt.table(name="gold_products")
def output():
    return spark.sql("SELECT * FROM LIVE.staging")
@dlt.table
def staging():
    return spark.sql("SELECT * FROM silver.products")
'''
        result = self.convert(source, {"target_mappings": {
            "gold_products": "gold.gold_products", "staging": "silver.staging"
        }})
        statements = self.sql(result)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("CREATE MATERIALIZED LAKE VIEW silver.staging"))
        self.assertIn("FROM silver.staging", statements[1])

    def test_replace_existing_requires_explicit_true(self):
        statement, = self.sql(self.convert(settings={"replace_existing": True}))
        self.assertTrue(statement.startswith("CREATE OR REPLACE MATERIALIZED LAKE VIEW"))

    def test_rejected_batch_units_do_not_partially_rewrite_dlt(self):
        cases = [
            (BATCH, {"target_mappings": {}}),
            (BATCH, {"source_mappings": {}}),
            (BATCH.replace("my_rules)", "dynamic_rules)"), {}),
            (BATCH.replace('"price >= 0"', '"price >= 0; DROP TABLE products"'), {}),
            (BATCH.replace("@dlt.table()", '@dlt.table(partition_cols=["product_id"])'), {}),
            (BATCH.replace("@dlt.table()", "@dlt.table()\\n@other".replace("\\n", "\n")), {}),
            (BATCH.replace("def gold_products():", "def gold_products(arg):"), {}),
            (BATCH.replace('return spark.sql("""', 'return spark.sql(f"""'), {}),
            (BATCH.replace("SELECT product_id, product_name, price", "WITH x AS (SELECT 1) SELECT *"), {}),
            (BATCH.replace("FROM LIVE.silver_products", "FROM (SELECT * FROM silver.products)"), {}),
            (BATCH.replace("FROM LIVE.silver_products", "FROM silver.a, silver.b"), {}),
            (BATCH.replace("FROM LIVE.silver_products", "FROM silver.products()"), {}),
            (BATCH.replace("FROM LIVE.silver_products", "FROM gold.gold_products"), {}),
            (BATCH.replace("FROM LIVE.silver_products", "FROM _a2f_view_0"), {}),
            (BATCH + '\nprint("side effect")', {}),
            (BATCH + "\nmy_rules = {}", {}),
            (BATCH.replace("import dlt", "import dlt as pipeline"), {}),
            (BATCH + "\n# COMMAND ----------\n# MAGIC %sql\n# MAGIC SELECT 1", {}),
            (BATCH.replace('return spark.sql("""', 'return other.sql("""'), {}),
        ]
        for source, settings in cases:
            with self.subTest(source=source[-80:], settings=settings):
                result = self.convert(source, settings)
                code = "\n".join("\n".join(lines) for _, kind, lines in result[0])
                self.assertIn("import dlt", code)
                self.assertNotIn("spark.sql('CREATE", code)
                self.assertTrue(any(i.level == "MANUAL" for i in result[1]))
                self.assertIn("dlt", result[2].checklist)
                self.assertTrue(result[5])
                self.assertFalse(any(i.rule_id == "X3-IMPORT" and i.level == "AUTO" for i in result[1]))

    def test_cyclic_views_and_view_expectations_are_rejected(self):
        source = '''import dlt
@dlt.table
def gold_products():
    return spark.sql("SELECT * FROM LIVE.a")
@dlt.view
def a():
    return spark.sql("SELECT * FROM LIVE.b")
@dlt.view
def b():
    return spark.sql("SELECT * FROM LIVE.a")
'''
        result = self.convert(source)
        self.assertIn("Cyclic", str(result[5]))
        source = BATCH.replace("@dlt.table()", "@dlt.view()")
        self.assertIn("Expectations on logical views", str(self.convert(source)[5]))

    def test_streaming_and_scd2_are_not_relabelled_batch(self):
        source = '''import dlt
rules = {"valid": "product_id IS NOT NULL"}
@dlt.table()
@dlt.expect_all_or_drop(rules)
def staging():
    return spark.readStream.table("silver.products")
@dlt.view
def logical():
    return spark.readStream.table("staging")
dlt.create_streaming_table("DimProducts")
dlt.apply_changes(target="DimProducts", source="logical", keys=["product_id"], sequence_by="product_id", stored_as_scd_type=2)
'''
        result = self.convert(source)
        items = result[1]
        self.assertEqual({i.rule_id for i in items},
                         {"X3-IMPORT", "X3-TABLE", "X3-EXPECT", "X3-VIEW", "X3-STREAM", "X3-SCD2"})
        self.assertTrue(all(i.level == "MANUAL" for i in items))
        self.assertTrue(all("SCD2" in i.message for i in items))
        self.assertIn("dlt.apply_changes", str(result[0]))
        self.assertNotIn("CREATE MATERIALIZED LAKE VIEW", str(result[0]))
        self.assertIn("TODO[ADB2FABRIC]", str(result[0]))
        self.assertIn("SCD2", str(result[2].checklist))
        streaming = self.convert(source[:source.index("@dlt.view")])
        self.assertIn("Streaming DLT", str(streaming[5]))

    def test_shared_helper_and_feature_disabled(self):
        result = self.convert(standalone=False)
        self.assertIn("shared helper", str(result[5]))
        disabled = self.convert(settings={"enabled": False})
        self.assertTrue(any(i.rule_id == "X3" and i.level == "MANUAL" for i in disabled[1]))
        self.assertNotIn("spark.sql('CREATE", str(disabled[0]))

    def test_string_literals_in_sql_are_not_mapped_as_relations(self):
        source = BATCH.replace("product_id, product_name, price", "'LIVE.not_a_table' AS label, product_id, price")
        sql, = self.sql(self.convert(source))
        self.assertIn("'LIVE.not_a_table'", sql)

    def test_invalid_configuration(self):
        cases = [
            {"enabled": "true"}, {"replace_existing": "true"},
            {"target_mappings": {"gold_products": "gold.bad;DROP"}},
            {"target_mappings": {"gold_products": "unqualified"}},
            {"target_mappings": {"A": "gold.a", "a": "gold.b"}},
            {"target_mappings": {"a": "gold.shared", "b": "gold.shared"}},
            {"source_mappings": {"LIVE.silver": "silver.bad-name"}},
        ]
        for change in cases:
            with self.subTest(change=change):
                config = copy.deepcopy(CONFIG)
                config["dlt"].update(change)
                with self.assertRaises(ConfigError):
                    validate_config(config)

    def test_active_configuration_and_reports_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "adb2fabric", root / "adb2fabric",
                            ignore=shutil.ignore_patterns("__pycache__"))
            config = copy.deepcopy(CONFIG)
            config["dlt"]["target_mappings"] = {"gold_products": "gold.products"}
            config["dlt"]["source_mappings"] = {"LIVE.silver_products": "silver.products"}
            (root / "adb2fabric" / "conversion-rules.json").write_text(json.dumps(config), encoding="utf-8")
            source = root / "input"
            source.mkdir()
            (source / "products.py").write_text(HEADER + BATCH, encoding="utf-8")
            out = root / "output"
            proc = subprocess.run(
                [sys.executable, "-m", "adb2fabric", str(source), "-o", str(out), "--no-open"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = next(out.parent.glob(out.name + "_*"))
            report = json.loads((out / "_report" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["manual_items"], 0)
            self.assertEqual(report["totals"]["auto"], 2)
            self.assertEqual(report["totals"]["review_items"], 1)
            self.assertEqual(report["notebooks"][0]["status"], "Converted - review")
            self.assertIn("X3-EXPECT", (out / "_report" / "items.csv").read_text(encoding="utf-8-sig"))
            self.assertEqual(len((out / "_report" / "manual_work.csv").read_text(encoding="utf-8-sig").splitlines()), 1)
            nb = json.loads((out / "products.ipynb").read_text(encoding="utf-8"))
            code = "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
            statements = []
            exec(code, {"spark": SimpleNamespace(sql=statements.append)})
            self.assertIn("CREATE MATERIALIZED LAKE VIEW gold.products", statements[0])
            self.assertIn("FROM silver.products", statements[0])
            self.assertNotIn("TODO[ADB2FABRIC]", code)


if __name__ == "__main__":
    unittest.main()
