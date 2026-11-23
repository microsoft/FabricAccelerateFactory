import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, validate_config
from adb2fabric.converter import convert_text
from adb2fabric.model import AUTO, MANUAL, REVIEW
from adb2fabric.paths import map_paths
from adb2fabric.rules import build_rules


def convert(body, language="python"):
    header = "# Databricks notebook source\n" if language == "python" else "-- Databricks notebook source\n"
    return convert_text(header + body, "sample.py", language)


def code(cells):
    return "\n".join("\n".join(lines) for _, kind, lines in cells if kind == "code")


class NotebookOrchestration(unittest.TestCase):
    def test_multiline_run_maps_target_and_timeout_and_preserves_arguments(self):
        body = '''result = dbutils.notebook.run(
    "/Shared/ETL/silver_transform",
    timeout=600,  # preserve this comment
    arguments={"date": "2024-01-01", "env": "prod"}
)
status = result
'''
        with patch.dict(CONFIG, notebook_mappings={"/Shared/ETL/silver_transform": "silver_transform"}):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        calls = []

        class FakeNotebook:
            def run(self, path, timeout_seconds=90, arguments=None, workspace=""):
                calls.append((path, timeout_seconds, arguments, workspace))
                return "transform_complete"

        namespace = {"notebookutils": types.SimpleNamespace(notebook=FakeNotebook())}
        exec(converted, namespace)
        self.assertEqual(namespace["status"], "transform_complete")
        self.assertEqual(calls, [("silver_transform", 600, {"date": "2024-01-01", "env": "prod"}, "")])
        self.assertIn("timeout_seconds=600", converted)
        self.assertIn("# preserve this comment", converted)
        finding = next(i for i in items if i.rule_id == "N1")
        self.assertEqual(finding.level, REVIEW)
        self.assertIn("notebookutils.notebook.run", finding.converted)
        self.assertIn("silver_transform", finding.converted)
        self.assertIn("run", ctx.checklist)

    def test_unmapped_and_dynamic_targets_are_not_guessed(self):
        body = 'a = dbutils.notebook.run("/Shared/ETL/child", 30, {})\nb = dbutils.notebook.run(target, timeoutSeconds=40)\n'
        with patch.dict(CONFIG, notebook_mappings={}):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        self.assertIn('"/Shared/ETL/child"', converted)
        self.assertIn("notebookutils.notebook.run(target, timeout_seconds=40)", converted)
        self.assertEqual([i.level for i in items if i.rule_id == "N1"], [REVIEW, REVIEW])
        self.assertTrue(any("notebook_mappings" in i.message for i in items if i.rule_id == "N1"))
        self.assertTrue(any("Dynamic notebook target" in i.message for i in items if i.rule_id == "N1"))

    def test_unsupported_run_signatures_stay_manual(self):
        calls = (
            'dbutils.notebook.run("child", 30, {}, retry=2)',
            'dbutils.notebook.run("child", 30, timeout=40)',
            'dbutils.notebook.run(*args)',
            'dbutils.notebook.run(arguments={})',
        )
        for call in calls:
            with self.subTest(call=call):
                cells, items, *_ = convert("result = " + call)
                finding = next(i for i in items if i.rule_id == "N1")
                self.assertEqual(finding.level, MANUAL)
                self.assertIn(call, code(cells))
                self.assertIn("TODO[ADB2FABRIC] N1", code(cells))

    def test_multiple_calls_and_string_literals_are_not_corrupted(self):
        body = '''text = 'dbutils.notebook.run("/Shared/x", 30)'
a = dbutils.notebook.run("/Shared/x", 30); b = dbutils.notebook.run("/Shared/y", timeout=40)
'''
        with patch.dict(CONFIG, notebook_mappings={"/Shared/x": "X", "/Shared/y": "Y"}):
            cells, items, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        self.assertIn('text = \'dbutils.notebook.run("/Shared/x", 30)\'', converted)
        self.assertEqual(converted.count("notebookutils.notebook.run("), 2)
        self.assertEqual(len([i for i in items if i.rule_id == "N1"]), 2)
        self.assertFalse(any(i.rule_id == "Z9" for i in items))

    def test_percent_run_uses_the_same_explicit_mapping(self):
        with patch.dict(CONFIG, notebook_mappings={"../shared/Common": "SharedCommon"}):
            cells, items, ctx, *_ = convert("# MAGIC %run ../shared/Common\n")
        self.assertEqual(cells[0][2], ["%run SharedCommon"])
        self.assertEqual(next(i.converted for i in items if i.rule_id == "MAG-RUN"), "%run SharedCommon")
        self.assertTrue(any("SharedCommon" in x for x in ctx.checklist["run"]))

    def test_percent_run_quotes_names_with_spaces_and_keeps_parameter_warning(self):
        body = '# MAGIC %run "/Shared/My Child" $parameter="value"\n'
        with patch.dict(CONFIG, notebook_mappings={"/Shared/My Child": "Fabric Child"}):
            cells, items, *_ = convert(body)
        self.assertEqual(cells[0][2], ['%run "Fabric Child"'])
        self.assertEqual(next(i.converted for i in items if i.rule_id == "MAG-RUN"), '%run "Fabric Child"')
        self.assertTrue(any(i.level == MANUAL for i in items))

    def test_custom_run_rule_is_not_overridden(self):
        data = copy.deepcopy(CONFIG)
        next(rule for rule in data["rules"] if rule["id"] == "N1")["replacement"] = "custom.run("
        with patch("adb2fabric.converter.RULES", build_rules(data)):
            cells, *_ = convert('x = dbutils.notebook.run("child", 30)')
        self.assertIn('custom.run("child", 30)', code(cells))

    def test_unparseable_cell_does_not_apply_unguarded_run_or_move(self):
        body = '''%matplotlib inline
result = dbutils.notebook.run("/Shared/ETL/child", timeout=600)
dbutils.fs.mv("old", "new", True)
df = spark.table("prod.silver.customers")
'''
        with patch.dict(CONFIG, notebook_mappings={"/Shared/ETL/child": "FabricChild"},
                        table_mappings={"prod.silver.customers": "silver.customers"}):
            cells, items, *_ = convert(body)
        converted = code(cells)
        self.assertIn('%matplotlib inline', converted)
        self.assertIn('dbutils.notebook.run("/Shared/ETL/child", timeout=600)', converted)
        self.assertIn('dbutils.fs.mv("old", "new", True)', converted)
        self.assertIn('spark.table("prod.silver.customers")', converted)
        self.assertNotIn("notebookutils.fs.mv", converted)
        self.assertTrue(any(i.rule_id == "P-CELL" and i.level == MANUAL for i in items))
        self.assertNotIn("%matplotlib inline  # TODO", converted)


class CatalogMappings(unittest.TestCase):
    def mappings(self):
        return {
            "prod.silver.customers": "silver.customers",
            "prod.silver.orders": "silver.orders",
            "prod.gold.customer_orders": "gold.customer_orders",
        }

    def test_python_spark_read_write_and_delta_table_mapping(self):
        body = '''customers = spark.read.table("prod.silver.customers")
orders = spark.read.format("delta").table("PROD.SILVER.ORDERS")
result.write.format("delta").mode("overwrite").saveAsTable("prod.gold.customer_orders")
target = DeltaTable.forName(spark, "prod.silver.customers")
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        self.assertIn("spark.read.table('silver.customers')", converted)
        self.assertIn(".table('silver.orders')", converted)
        self.assertIn(".saveAsTable('gold.customer_orders')", converted)
        self.assertIn("DeltaTable.forName(spark, 'silver.customers')", converted)
        self.assertEqual([i.level for i in items if i.rule_id == "T-UC"], [AUTO] * 4)
        self.assertIn("unity", ctx.checklist)
        self.assertIn("lakehouse", ctx.checklist)

    def test_identity_table_mappings_are_auto_for_sql_reads_and_delta_writes(self):
        body = '''a = spark.sql("SELECT * FROM prod.gold.customers")
b = spark.sql("SELECT * FROM prod.gold.products")
c = spark.table("prod.gold.orders")
d = DeltaTable.forName(spark, "prod.gold.orders")
df.write.format("delta").saveAsTable("prod.gold.orders")
'''
        names = ("prod.gold.customers", "prod.gold.products", "prod.gold.orders")
        with patch.dict(CONFIG, table_mappings={name: name for name in names}):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        self.assertIn("SELECT * FROM prod.gold.customers", converted)
        self.assertIn("SELECT * FROM prod.gold.products", converted)
        self.assertEqual(converted.count("prod.gold.orders"), 3)
        self.assertEqual([i.level for i in items if i.rule_id == "T-UC"], [AUTO] * 5)
        self.assertFalse(any(i.level in (REVIEW, MANUAL) for i in items))
        self.assertNotIn("unity", ctx.checklist)
        self.assertIn("lakehouse", ctx.checklist)
        self.assertTrue(all("identity mapping" in i.message for i in items if i.rule_id == "T-UC"))

    def test_three_part_unmapped_and_dynamic_not_silently_counted_as_converted(self):
        body = 'a = spark.table("prod.silver.customers")\nb = spark.table(f"prod.silver.{name}")\n'
        with patch.dict(CONFIG, table_mappings={}):
            cells, items, *_ = convert(body)
        self.assertIn('"prod.silver.customers"', code(cells))
        flags = [i for i in items if i.rule_id == "T-UC"]
        self.assertEqual([i.level for i in flags], [REVIEW, MANUAL])
        self.assertIn("TODO[ADB2FABRIC]", code(cells))

    def test_dynamic_table_arguments_and_sql_are_manual(self):
        body = '''table_name = "prod.silver.orders"
spark.table(table_name)
spark.read.table(f"{catalog}.{schema}.{table}")
spark.sql(f"SELECT * FROM prod.silver.{table}")
spark.sql(query)
'''
        cells, items, *_ = convert(body)
        converted = code(cells)
        self.assertIn("spark.table(table_name)", converted)
        self.assertIn('spark.read.table(f"{catalog}.{schema}.{table}")', converted)
        self.assertIn('spark.sql(f"SELECT * FROM prod.silver.{table}")', converted)
        self.assertEqual([i.level for i in items if i.rule_id == "T-UC"], [MANUAL] * 4)

    def test_literal_spark_sql_keyword_is_mapped(self):
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert('spark.sql(sqlQuery="SELECT * FROM prod.silver.orders")')
        self.assertIn('sqlQuery="SELECT * FROM silver.orders"', code(cells))
        self.assertTrue(any(i.rule_id == "T-UC" and i.level == AUTO for i in items))

    def test_ambiguous_spark_sql_query_arguments_are_not_rewritten(self):
        body = 'spark.sql("SELECT * FROM prod.silver.orders", sqlQuery="SELECT * FROM prod.silver.customers")'
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        self.assertIn(body, code(cells))
        self.assertTrue(any(i.rule_id == "T-UC" and i.level == MANUAL for i in items))

    def test_simple_dynamic_bronze_table_requires_review_not_manual(self):
        body = 'df.writeStream.toTable(f"bronze.{p_file_name}")'
        cells, items, *_ = convert(body)
        self.assertIn(body, code(cells))
        finding = next(i for i in items if i.rule_id == "T-UC")
        self.assertEqual(finding.level, REVIEW)
        self.assertIn("runtime table identifier", finding.message)

    def test_unverified_writer_alias_and_unrelated_strings_do_not_rewrite(self):
        body = '''example = "prod.silver.customers"
logger.info("prod.silver.customers")
writer.saveAsTable("prod.silver.customers")
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        self.assertEqual(code(cells).count('"prod.silver.customers"'), 3)
        self.assertEqual([i.level for i in items if i.rule_id == "T-UC"], [MANUAL])

    def test_table_finding_does_not_hide_unrelated_unsupported_dbutils(self):
        body = 'a = spark.table("prod.silver.customers"); dbutils.unknown()'
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        self.assertTrue(any(i.rule_id == "T-UC" and i.level == AUTO for i in items))
        self.assertTrue(any(i.rule_id == "Z9" and i.level == MANUAL for i in items))
        self.assertIn("TODO[ADB2FABRIC]", code(cells))

    def test_sql_magic_maps_only_sql_table_references(self):
        body = '''# MAGIC %sql
# MAGIC SELECT 'prod.silver.orders' AS label FROM prod.silver.customers c
# MAGIC JOIN `prod`.`silver`.`orders` o ON c.id=o.id -- prod.gold.customer_orders
# MAGIC INSERT INTO prod.gold.customer_orders SELECT * FROM prod.silver.orders
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, ctx, *_ = convert(body)
        converted = "\n".join(cells[0][2])
        self.assertIn("'prod.silver.orders' AS label FROM silver.customers", converted)
        self.assertIn("JOIN `silver`.`orders`", converted)
        self.assertIn("-- prod.gold.customer_orders", converted)
        self.assertIn("INSERT INTO gold.customer_orders SELECT * FROM silver.orders", converted)
        self.assertEqual(sum(i.rule_id == "T-UC" and i.level == AUTO for i in items), 4)
        self.assertIn("unity", ctx.checklist)

    def test_embedded_multiline_sql_mapping_preserves_sql_literals_and_comments(self):
        body = '''query = spark.sql("""
SELECT 'prod.silver.orders' AS label
FROM prod.silver.customers
JOIN prod.silver.orders ON 1 = 1
""")
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        self.assertIn("FROM silver.customers", converted)
        self.assertIn("JOIN silver.orders", converted)
        self.assertIn("'prod.silver.orders'", converted)
        self.assertNotIn("REVIEW[ADB2FABRIC]", converted.split('"""')[1])
        self.assertEqual(sum(i.rule_id == "T-UC" for i in items), 2)

    def test_merge_and_comma_join_map_all_table_references(self):
        body = '''# MAGIC %sql
# MAGIC MERGE INTO prod.gold.customer_orders t USING prod.silver.orders s
# MAGIC ON t.id=s.id WHEN MATCHED THEN UPDATE SET *
# MAGIC ;
# MAGIC SELECT concat('prod.silver.orders', x) FROM prod.silver.customers c,
# MAGIC prod.silver.orders o WHERE c.id=o.id
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        converted = "\n".join(cells[0][2])
        self.assertIn("MERGE INTO gold.customer_orders t USING silver.orders s", converted)
        self.assertIn("FROM silver.customers c,", converted)
        self.assertIn("silver.orders o WHERE", converted)
        self.assertIn("concat('prod.silver.orders', x)", converted)
        self.assertEqual(sum(i.rule_id == "T-UC" and i.level == AUTO for i in items), 4)

    def test_sql_comma_state_respects_subqueries_clauses_and_join_using(self):
        body = '''# MAGIC %sql
# MAGIC SELECT * FROM (SELECT * FROM prod.silver.customers) c, prod.silver.orders o
# MAGIC JOIN prod.gold.customer_orders g USING (id)
# MAGIC WHERE array_contains(array(1, 2), 1)
# MAGIC ;
# MAGIC SELECT prod.silver.customers, prod.silver.orders FROM local_table
'''
        with patch.dict(CONFIG, table_mappings=self.mappings()):
            cells, items, *_ = convert(body)
        converted = "\n".join(cells[0][2])
        self.assertIn("FROM silver.customers) c, silver.orders o", converted)
        self.assertIn("JOIN gold.customer_orders g USING (id)", converted)
        self.assertIn("SELECT prod.silver.customers, prod.silver.orders FROM local_table", converted)
        self.assertEqual(sum(i.rule_id == "T-UC" for i in items), 3)

    def test_unmapped_merge_source_is_reported(self):
        body = "# MAGIC %sql\n# MAGIC MERGE INTO prod.gold.customer_orders t USING prod.silver.orders s ON t.id=s.id"
        with patch.dict(CONFIG, table_mappings={"prod.gold.customer_orders": "gold.customer_orders"}):
            cells, items, *_ = convert(body)
        self.assertIn("USING prod.silver.orders", "\n".join(cells[0][2]))
        self.assertEqual([i.level for i in items if i.rule_id == "T-UC"], [AUTO, REVIEW])

    def test_escaped_python_sql_and_comment_spanning_identifier_remain_manual(self):
        cases = [
            'spark.sql("SELECT * FROM prod.silver.customers\\n")',
            '# MAGIC %sql\n# MAGIC SELECT * FROM prod/* keep */.silver.customers\n',
        ]
        for body in cases:
            with self.subTest(body=body), patch.dict(CONFIG, table_mappings=self.mappings()):
                cells, items, *_ = convert(body)
                finding = next(i for i in items if i.rule_id == "T-UC")
                self.assertEqual(finding.level, MANUAL)
                self.assertIn("TODO[ADB2FABRIC]", "\n".join("\n".join(c[2]) for c in cells))


class TempAndTablePaths(unittest.TestCase):
    def mappings(self):
        return [{"id": "tmp", "source": "/tmp", "target": "Files/temp", "scope": "dbutils_fs"}]

    def test_scoped_tmp_mapping_does_not_change_local_paths_or_file_content(self):
        body = '''dbutils.fs.put("/tmp/checkpoint.txt", "/tmp/this-is-file-content", overwrite=True)
content = dbutils.fs.head("/tmp/checkpoint.txt")
local = open("/tmp/checkpoint.txt")
df = spark.read.parquet("/tmp/checkpoint.txt")
'''
        with patch.dict(CONFIG, path_mappings=self.mappings()):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        self.assertIn('notebookutils.fs.put("Files/temp/checkpoint.txt", "/tmp/this-is-file-content"', converted)
        self.assertIn('notebookutils.fs.head("Files/temp/checkpoint.txt")', converted)
        self.assertIn('open("/tmp/checkpoint.txt")', converted)
        self.assertIn('spark.read.parquet("/tmp/checkpoint.txt")', converted)
        self.assertEqual(sum(i.rule_id == "P-MAP" for i in items), 2)
        self.assertFalse(any(i.rule_id == "P-TMP" for i in items))
        self.assertIn("lakehouse", ctx.checklist)

    def test_unmapped_tmp_is_manual_including_percent_fs(self):
        for body in ('dbutils.fs.head("/tmp/checkpoint.txt")', '# MAGIC %fs ls /tmp/checkpoint.txt'):
            with self.subTest(body=body), patch.dict(CONFIG, path_mappings=[]):
                cells, items, *_ = convert(body)
                self.assertTrue(any(i.rule_id == "P-TMP" and i.level == MANUAL for i in items))
                self.assertIn("TODO[ADB2FABRIC]", code(cells))

    def test_scoped_mapping_handles_copy_destinations_and_fstrings(self):
        with patch.dict(CONFIG, path_mappings=self.mappings()):
            result, evidence = map_paths(['dbutils.fs.cp(f"/tmp/{name}", "/tmp/dest", True)',
                                          'x = f"/tmp/{name}"'], "code")
            sql, sql_evidence = map_paths(["SELECT '/tmp/dest'"], "sql")
        self.assertEqual(result[0].count("Files/temp"), 2)
        self.assertEqual(result[1], 'x = f"/tmp/{name}"')
        self.assertEqual(len(evidence[0]), 2)
        self.assertEqual(sql, ["SELECT '/tmp/dest'"])
        self.assertEqual(sql_evidence, {})

    def test_explicit_tables_destination_adds_lakehouse_checklist(self):
        mapping = [{"id": "delta", "source": "/mnt/silver", "target": "Tables"}]
        # A specific path is required; mapping all of Tables is deliberately not implicit.
        mapping[0]["target"] = "Tables/silver"
        with patch.dict(CONFIG, path_mappings=mapping):
            cells, items, ctx, *_ = convert('df = spark.read.format("delta").load("/mnt/silver/transactions")')
        self.assertIn('"Tables/silver/transactions"', code(cells))
        self.assertEqual([i.level for i in items if i.rule_id == "P-MAP"], [AUTO])
        self.assertIn("lakehouse", ctx.checklist)

    def test_basic_move_is_review_and_recursive_move_is_not_misconverted(self):
        cells, items, *_ = convert('dbutils.fs.mv("old", "new")')
        self.assertIn('notebookutils.fs.mv("old", "new")', code(cells))
        self.assertEqual(next(i.level for i in items if i.rule_id == "F-MV"), REVIEW)
        for call in ('dbutils.fs.mv("old", "new", True)', 'dbutils.fs.mv("old", "new", recurse=True)'):
            with self.subTest(call=call):
                cells, items, *_ = convert(call)
                self.assertIn(call, code(cells))
                self.assertNotIn("notebookutils.fs.mv", code(cells))
                self.assertTrue(any(i.rule_id == "F-MV" and i.level == MANUAL for i in items))
        cells, items, *_ = convert("# MAGIC %fs mv -r old new")
        self.assertTrue(any(i.rule_id == "MAG-FS" and i.level == MANUAL for i in items))
        self.assertNotIn("notebookutils.fs.mv", code(cells))

    def test_move_report_shows_mapped_paths(self):
        with patch.dict(CONFIG, path_mappings=self.mappings()):
            cells, items, *_ = convert('dbutils.fs.mv("/tmp/a", "/tmp/b")')
        finding = next(i for i in items if i.rule_id == "F-MV")
        self.assertIn("Files/temp/a", finding.converted)
        self.assertIn("Files/temp/b", finding.converted)
        self.assertIn("notebookutils.fs.mv", code(cells))

    def test_manual_move_report_and_magic_report_show_final_mapped_paths(self):
        with patch.dict(CONFIG, path_mappings=self.mappings()):
            cells, items, *_ = convert('dbutils.fs.mv("/tmp/a", "/tmp/b", True)')
            finding = next(i for i in items if i.rule_id == "F-MV")
            self.assertEqual(finding.level, MANUAL)
            self.assertIn('dbutils.fs.mv("Files/temp/a", "Files/temp/b", True)', finding.converted)
            cells, items, *_ = convert("# MAGIC %fs ls /tmp/checkpoint.txt")
            finding = next(i for i in items if i.rule_id == "MAG-FS")
            self.assertIn("Files/temp/checkpoint.txt", finding.converted)

    def test_move_method_alias_is_not_auto_converted(self):
        cells, items, *_ = convert('move = dbutils.fs.mv\nmove("old", "new", True)')
        self.assertIn("move = dbutils.fs.mv", code(cells))
        self.assertNotIn("notebookutils.fs.mv", code(cells))
        self.assertTrue(any(i.rule_id == "F-MV" and i.level == MANUAL for i in items))


class MlflowMigration(unittest.TestCase):
    def settings(self):
        return {"use_fabric_default_tracking": True,
                "experiment_mappings": {"/Users/example/Experiment": "FabricExperiment"}}

    def test_tracking_and_experiment_mapping_preserve_other_mlflow_code(self):
        body = '''import mlflow
mlflow.set_tracking_uri("databricks")
mlflow.set_experiment("/Users/example/Experiment")
mlflow.log_metric("accuracy", 0.95)
'''
        with patch.dict(CONFIG, mlflow=self.settings()):
            cells, items, ctx, *_ = convert(body)
        converted = code(cells)
        ast.parse(converted)
        events = []
        fake = types.ModuleType("mlflow")
        fake.set_tracking_uri = lambda uri: events.append(("tracking", uri))
        fake.set_experiment = lambda name: events.append(("experiment", name))
        fake.log_metric = lambda name, value: events.append(("metric", name, value))
        with patch.dict(sys.modules, {"mlflow": fake}):
            exec(converted, {})
        self.assertEqual(events, [("experiment", "FabricExperiment"), ("metric", "accuracy", 0.95)])
        self.assertNotIn('set_tracking_uri("databricks")', converted)
        self.assertEqual([(i.rule_id, i.level) for i in items if i.rule_id.startswith("ML")],
                         [("ML1", REVIEW), ("ML2", REVIEW)])
        self.assertIn("mlflow", ctx.checklist)

    def test_unmapped_experiment_and_unsupported_tracking_are_manual(self):
        cases = [
            ('import mlflow\nmlflow.set_experiment("/Users/example/unknown")', "ML2"),
            ('import mlflow\nx = mlflow.set_tracking_uri("databricks")', "ML1"),
            ('import mlflow\nif enabled:\n    mlflow.set_tracking_uri("databricks")', "ML1"),
            ('import mlflow\nmlflow.set_registry_uri("databricks-uc")', "ML1"),
            ('import mlflow\nmlflow.set_tracking_uri("databricks", **options)', "ML1"),
        ]
        with patch.dict(CONFIG, mlflow=self.settings()):
            for body, rule in cases:
                with self.subTest(body=body):
                    cells, items, *_ = convert(body)
                    self.assertTrue(any(i.rule_id == rule and i.level == MANUAL for i in items))
                    self.assertIn("TODO[ADB2FABRIC]", code(cells))

    def test_tracking_feature_is_gated_and_aliases_resolve_across_cells(self):
        body = 'import mlflow as mf\n# COMMAND ----------\nmf.set_tracking_uri("databricks")\n'
        for enabled, expected in ((True, REVIEW), (False, MANUAL)):
            settings = self.settings()
            settings["use_fabric_default_tracking"] = enabled
            with self.subTest(enabled=enabled), patch.dict(CONFIG, mlflow=settings):
                cells, items, *_ = convert(body)
                finding = next(i for i in items if i.rule_id == "ML1")
                self.assertEqual(finding.level, expected)
                self.assertEqual('mf.set_tracking_uri("databricks")' in code(cells), not enabled)

    def test_keyword_uri_and_import_order_are_handled_safely(self):
        with patch.dict(CONFIG, mlflow=self.settings()):
            cells, items, *_ = convert('import mlflow\nmlflow.set_tracking_uri(uri="databricks")\n')
            self.assertEqual(next(i.level for i in items if i.rule_id == "ML1"), REVIEW)
            self.assertNotIn("set_tracking_uri", code(cells))
            cells, items, *_ = convert('mlflow.set_tracking_uri("databricks")\nimport mlflow\n')
            self.assertEqual(next(i.level for i in items if i.rule_id == "ML1"), MANUAL)
            self.assertIn('mlflow.set_tracking_uri("databricks")', code(cells))

    def test_compatible_tracking_uris_and_simple_experiment_names_are_preserved(self):
        body = '''import mlflow
mlflow.set_tracking_uri("https://example.test")
mlflow.set_experiment("ExistingExperiment")
mlflow.log_metric("x", 1)
'''
        cells, items, *_ = convert(body)
        self.assertIn('set_tracking_uri("https://example.test")', code(cells))
        self.assertIn('set_experiment("ExistingExperiment")', code(cells))
        self.assertFalse(any(i.rule_id.startswith("ML") for i in items))

    def test_dynamic_name_and_existing_id_require_review(self):
        body = 'import mlflow\nmlflow.set_experiment(name)\nmlflow.set_experiment(experiment_id="123")\n'
        cells, items, *_ = convert(body)
        self.assertEqual([i.level for i in items if i.rule_id == "ML2"], [REVIEW, REVIEW])
        self.assertIn("mlflow.set_experiment(name)", code(cells))

    def test_dynamic_tracking_and_registry_uris_are_manual(self):
        body = 'import mlflow\nmlflow.set_tracking_uri(uri)\nmlflow.set_registry_uri(f"databricks://{profile}")\n'
        cells, items, *_ = convert(body)
        self.assertEqual([i.level for i in items if i.rule_id == "ML1"], [MANUAL, MANUAL])
        self.assertIn("mlflow.set_tracking_uri(uri)", code(cells))
        self.assertIn('mlflow.set_registry_uri(f"databricks://{profile}")', code(cells))

    def test_named_databricks_tracking_profile_is_not_silently_removed(self):
        body = 'import mlflow\nmlflow.set_tracking_uri("databricks://remote-profile")'
        with patch.dict(CONFIG, mlflow=self.settings()):
            cells, items, *_ = convert(body)
        self.assertIn('mlflow.set_tracking_uri("databricks://remote-profile")', code(cells))
        self.assertEqual(next(i.level for i in items if i.rule_id == "ML1"), MANUAL)


class PatternConfiguration(unittest.TestCase):
    def test_invalid_checklist_is_reported_as_config_error(self):
        for value in (None, 42, [], "mlflow"):
            data = copy.deepcopy(CONFIG)
            data["checklist_text"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "checklist_text: expected an object"):
                validate_config(data)

    def test_invalid_mappings_are_rejected(self):
        changes = [
            ("notebook_mappings", {"/Shared/x": "Folder/Child"}),
            ("notebook_mappings", {"/Shared/x": ""}),
            ("table_mappings", {"prod.silver": "silver.table"}),
            ("table_mappings", {"prod.silver.customers": "silver.bad-name"}),
            ("table_mappings", {"prod.silver.a": "silver.a", "other.silver.a": "SILVER.A"}),
            ("table_mappings", {"PROD.silver.a": "silver.a", "prod.SILVER.a": "silver.b"}),
            ("mlflow", {"use_fabric_default_tracking": "true", "experiment_mappings": {}}),
            ("mlflow", {"use_fabric_default_tracking": True, "experiment_mappings": {"/Users/x": "folder/name"}}),
            ("path_mappings", [{"id": "tmp", "source": "/tmp", "target": "Files/temp"}]),
            ("path_mappings", [{"id": "tmp", "source": "/tmp", "target": "Files/temp", "scope": "unknown"}]),
        ]
        for section, value in changes:
            data = copy.deepcopy(CONFIG)
            data[section] = value
            with self.subTest(section=section, value=value), self.assertRaises(ConfigError):
                validate_config(data)
        validate_config(CONFIG)

    def test_json_mappings_drive_the_real_cli_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "adb2fabric"
            shutil.copytree(ROOT / "adb2fabric", package, ignore=shutil.ignore_patterns("__pycache__"))
            config = copy.deepcopy(CONFIG)
            config["notebook_mappings"] = {"/Shared/ETL/child": "FabricChild"}
            config["table_mappings"] = {"prod.silver.customers": "silver.customers"}
            config["mlflow"] = {"use_fabric_default_tracking": True,
                                "experiment_mappings": {"/Users/example/Exp": "FabricExperiment"}}
            config["path_mappings"] = [{"id": "tmp", "source": "/tmp", "target": "Files/temp",
                                        "scope": "dbutils_fs"}]
            (package / "conversion-rules.json").write_text(json.dumps(config), encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "integration.py").write_text('''# Databricks notebook source
import mlflow
mlflow.set_tracking_uri("databricks")
mlflow.set_experiment("/Users/example/Exp")
dbutils.fs.put("/tmp/output", "data", overwrite=True)
result = dbutils.notebook.run("/Shared/ETL/child", timeout=600, arguments={"date": "2024"})
df = spark.read.table("prod.silver.customers")
# COMMAND ----------
# MAGIC %sql
# MAGIC SELECT * FROM prod.silver.customers
''', encoding="utf-8")
            output = root / "converted"
            proc = subprocess.run([sys.executable, "-m", "adb2fabric", str(source), "-o", str(output), "--no-open"],
                                  cwd=root, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            output = next(output.parent.glob(output.name + "_*"))
            report = json.loads((output / "_report" / "report.json").read_text(encoding="utf-8"))
            notebook = json.loads((output / "integration.ipynb").read_text(encoding="utf-8"))
            body = "\n".join("".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code")
            self.assertIn("notebookutils.notebook.run('FabricChild', timeout_seconds=600", body)
            self.assertIn("spark.read.table('silver.customers')", body)
            self.assertIn("SELECT * FROM silver.customers", body)
            self.assertIn('"Files/temp/output"', body)
            self.assertIn("mlflow.set_experiment('FabricExperiment')", body)
            self.assertNotIn('set_tracking_uri("databricks")', body)
            self.assertEqual(report["notebooks"][0]["status"], "Converted - review")
            self.assertTrue({"ML1", "ML2", "N1", "T-UC", "P-MAP"} <=
                            {item["rule_id"] for item in report["notebooks"][0]["items"]})


if __name__ == "__main__":
    unittest.main()
