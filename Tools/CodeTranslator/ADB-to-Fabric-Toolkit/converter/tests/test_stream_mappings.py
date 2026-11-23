import ast
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, validate_config
from adb2fabric.converter import build_notebook, convert_text
from adb2fabric.model import AUTO, MANUAL, REVIEW


SOURCE = '''# Databricks notebook source
p_file_name = dbutils.widgets.get("file_name")
# COMMAND ----------
df = spark.readStream.format("cloudFiles")\\
    .option("cloudFiles.format", "parquet")\\
    .option("cloudFiles.schemaLocation", f"dbfs:/checkpoints/schema/{p_file_name}")\\
    .load(f"abfss://source@account.dfs.core.windows.net/{p_file_name}*.parquet")
# COMMAND ----------
df.writeStream.format("delta")\\
    .outputMode("append")\\
    .option("checkpointLocation", f"dbfs:/checkpoints/write/{p_file_name}")\\
    .trigger(availableNow=True)\\
    .toTable(f"bronze.{p_file_name}")
# COMMAND ----------
df = spark.table(f"bronze.{p_file_name}")
display(df)
'''


def convert(text=SOURCE):
    return convert_text(text, "Bronze_Layer.py", "python")


def code(cells):
    return "\n".join("\n".join(lines) for _, kind, lines in cells if kind == "code")


class StreamMappings(unittest.TestCase):
    def setUp(self):
        mapping = [{"id": "checkpoint-path", "source": "dbfs:/checkpoints",
                    "target": "Files/checkpoints"}]
        patcher = patch.dict(CONFIG, path_mappings=mapping)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self):
        return {"enabled": True, "schemas": {"Bronze_Layer.py": {"df": "product_id STRING, price DOUBLE"}}}

    def test_native_stream_preserves_glob_and_existing_sink_and_waits(self):
        with patch.dict(CONFIG, autoloader=self.settings()):
            cells, items, ctx, *_ = convert()
        result = code(cells)
        ast.parse(result)
        self.assertNotIn("cloudFiles", result)
        self.assertNotIn("checkpoints/schema", result)
        self.assertIn(".format('parquet').schema('product_id STRING, price DOUBLE')", result)
        self.assertIn("{p_file_name}*.parquet", result)
        self.assertIn("Files/checkpoints/write/{p_file_name}", result)
        self.assertEqual(result.count(".writeStream"), 1)
        self.assertEqual(result.count(".toTable("), 1)
        self.assertLess(result.index(".awaitTermination()"), result.index("spark.table"))
        self.assertEqual([(i.rule_id, i.level) for i in items if i.rule_id.startswith("X1")],
                         [("X1-READER", REVIEW), ("X1-WAIT", AUTO)])
        self.assertIn("autoloader", ctx.checklist)

    def test_generated_query_executes_sink_once_and_waits_before_read(self):
        events = []

        class FakeSpark:
            @property
            def readStream(self):
                return self

            @property
            def writeStream(self):
                return self

            def __getattr__(self, method):
                def call(*args, **kwargs):
                    events.append((method, args, kwargs))
                    return self
                return call

        with patch.dict(CONFIG, autoloader=self.settings(), parameter_defaults={"file_name": "products"}):
            cells, items, ctx, *_ = convert()
        exec(compile(code(cells), "<converted>", "exec"),
             {"spark": FakeSpark(), "file_name": "products", "display": lambda _: None})
        methods = [e[0] for e in events]
        self.assertEqual(methods.count("toTable"), 1)
        self.assertEqual(methods.count("awaitTermination"), 1)
        self.assertLess(methods.index("awaitTermination"), methods.index("table"))
        load = next(e for e in events if e[0] == "load")
        self.assertEqual(load[1], ("abfss://source@account.dfs.core.windows.net/products*.parquet",))

    def test_missing_schema_is_not_guessed(self):
        with patch.dict(CONFIG, autoloader={"enabled": True, "schemas": {}}):
            cells, items, ctx, *_ = convert()
        self.assertIn('format("cloudFiles")', code(cells))
        flags = [i for i in items if i.rule_id == "X1"]
        self.assertTrue(flags)
        self.assertTrue(all(i.level == MANUAL and "Schema resolution" in i.message for i in flags))
        self.assertIn("autoloader", ctx.checklist)

    def poc_settings(self):
        return {"enabled": True, "schemas": {}, "schema_mode": "runtime_parquet",
                "remove_checkpoint_resets": True}

    def with_resets(self, source=SOURCE):
        return source.replace("p_file_name = dbutils.widgets.get(\"file_name\")",
                              '''p_file_name = dbutils.widgets.get("file_name")
dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=True)
dbutils.fs.rm(f"dbfs:/checkpoints/schema/{p_file_name}", True)
print(f"Checkpoints cleared for: {p_file_name}")''')

    def test_poc_runtime_schema_and_exact_checkpoint_reset_removal(self):
        events = []
        schema = object()

        class FakeSpark:
            @property
            def read(self):
                return self

            @property
            def readStream(self):
                return self

            @property
            def writeStream(self):
                return self

            def __getattr__(self, method):
                def call(*args, **kwargs):
                    events.append((method, args))
                    return type("Batch", (), {"schema": schema})() if method == "parquet" else self
                return call

        with patch.dict(CONFIG, autoloader=self.poc_settings()):
            cells, items, ctx, *_ = convert(self.with_resets())
        converted = code(cells)
        ast.parse(converted)
        exec(converted, {"spark": FakeSpark(), "file_name": "products",
                         "print": lambda _: None, "display": lambda _: None})
        self.assertNotIn(".fs.rm", converted)
        self.assertNotIn("Checkpoints cleared", converted)
        self.assertNotIn("cloudFiles", converted)
        self.assertNotIn("checkpoints/schema", converted)
        self.assertEqual(converted.count(".writeStream"), 1)
        self.assertEqual(converted.count(".toTable("), 1)
        self.assertEqual(sum(i.rule_id == "X1-RESET" and i.level == AUTO for i in items), 2)
        self.assertEqual(sum(i.rule_id == "X1-RESET-LOG" for i in items), 1)
        self.assertTrue(any("POC runtime schema inference" in detail for detail in ctx.checklist["autoloader"]))
        self.assertFalse(any(i.rule_id == "X1" for i in items))
        batch = next(args for method, args in events if method == "parquet")
        stream = next(args for method, args in events if method == "load")
        self.assertEqual(batch, stream)
        self.assertEqual(stream, ("abfss://source@account.dfs.core.windows.net/products*.parquet",))
        self.assertIs(next(args[0] for method, args in events if method == "schema"), schema)
        methods = [method for method, _ in events]
        self.assertLess(methods.index("parquet"), methods.index("load"))
        self.assertLess(methods.index("awaitTermination"), methods.index("table"))
        wait = next(i for i in items if i.rule_id == "X1-WAIT")
        self.assertIn("Files/checkpoints/write", wait.converted)

    def test_poc_schema_errors_are_propagated_without_empty_schema_fallback(self):
        class FailingSpark:
            @property
            def readStream(self):
                return self

            @property
            def read(self):
                return self

            def format(self, _):
                return self

            def parquet(self, _):
                raise RuntimeError("Source missing or unreadable")

            def schema(self, _):
                raise AssertionError("A failed batch schema read must not start streaming")

        with patch.dict(CONFIG, autoloader=self.poc_settings()):
            cells, *_ = convert()
        with self.assertRaisesRegex(RuntimeError, "Source missing or unreadable"):
            exec(code(cells), {"spark": FailingSpark(), "file_name": "products"})

    def test_explicit_schema_takes_precedence_over_poc_mode(self):
        settings = self.poc_settings()
        settings["schemas"] = {"Bronze_Layer.py": {"df": "real_field STRING"}}
        with patch.dict(CONFIG, autoloader=settings):
            cells, *_ = convert(self.with_resets())
        self.assertIn(".schema('real_field STRING')", code(cells))
        self.assertNotIn("spark.read.parquet", code(cells))
        self.assertNotIn(".fs.rm", code(cells))

    def test_poc_does_not_remove_unrelated_broad_or_conditional_deletes(self):
        deletes = [
            'dbutils.fs.rm("dbfs:/checkpoints", True)',
            'dbutils.fs.rm("Files/unrelated", True)',
            'dbutils.fs.rm(checkpoint_path, True)',
            'if reset:\n    dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", True)',
            'deleted = dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", True)',
            'dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=recursive)',
        ]
        for deletion in deletes:
            source = SOURCE.replace("# COMMAND ----------", deletion + "\n# COMMAND ----------", 1)
            with self.subTest(deletion=deletion), patch.dict(CONFIG, autoloader=self.poc_settings()):
                cells, items, *_ = convert(source)
                self.assertIn('format("cloudFiles")', code(cells))
                self.assertIn(".fs.rm", code(cells))
                self.assertFalse(any(i.rule_id == "X1-RESET" for i in items))

    def test_poc_rejects_side_effecting_source_expressions(self):
        source = SOURCE.replace("{p_file_name}*.parquet", "{choose_dataset()}*.parquet")
        with patch.dict(CONFIG, autoloader=self.poc_settings()):
            cells, items, *_ = convert(source)
        self.assertIn('format("cloudFiles")', code(cells))
        self.assertTrue(any("simple variable substitutions" in i.message for i in items if i.rule_id == "X1"))

    def test_reader_without_schema_location_and_multiline_reset(self):
        source = self.with_resets().replace(
            '    .option("cloudFiles.schemaLocation", f"dbfs:/checkpoints/schema/{p_file_name}")\\\n', "")
        source = source.replace('dbutils.fs.rm(f"dbfs:/checkpoints/schema/{p_file_name}", True)\n', "")
        source = source.replace('dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=True)',
                                'dbutils.fs.rm(\n    f"dbfs:/checkpoints/write/{p_file_name}",\n    recurse=True\n)')
        with patch.dict(CONFIG, autoloader=self.poc_settings()):
            cells, items, *_ = convert(source)
        ast.parse(code(cells))
        self.assertIn("spark.read.parquet", code(cells))
        self.assertNotIn(".fs.rm", code(cells))
        self.assertEqual(sum(i.rule_id == "X1-RESET" for i in items), 1)

    def test_atomic_fallback_for_unsupported_stream_forms(self):
        variants = [
            SOURCE.replace('.option("cloudFiles.format", "parquet")',
                           '.option("cloudFiles.schemaEvolutionMode", "addNewColumns")'),
            SOURCE.replace('availableNow=True', 'processingTime="10 seconds"'),
            SOURCE.replace('.outputMode("append")', '.outputMode("complete")'),
            SOURCE.replace('dbfs:/checkpoints/write/', 'dbfs:/unknown/'),
            SOURCE.replace('df.writeStream', 'df = df.filter("x > 0")\ndf.writeStream'),
            SOURCE.replace('display(df)', 'dbutils.fs.rm("Files/checkpoints/write", True)\ndisplay(df)'),
            SOURCE.replace('df = spark.readStream', 'extra = 1; df = spark.readStream'),
            SOURCE.replace('df.writeStream', 'print("start"); df.writeStream'),
            SOURCE.replace('df = spark.readStream', 'if True:\n df = spark.readStream'),
            SOURCE.replace('display(df)', 'display(df)\n# COMMAND ----------\n# MAGIC %sql\n# MAGIC select 1'),
        ]
        with patch.dict(CONFIG, autoloader=self.settings()):
            for source in variants:
                with self.subTest(source=source):
                    cells, items, *_ = convert(source)
                    result = code(cells)
                    self.assertIn('format("cloudFiles")', result)
                    self.assertNotIn("awaitTermination", result)
                    self.assertFalse(any(i.rule_id == "X1-READER" for i in items))
                    self.assertTrue(any(i.rule_id == "X1" and i.level == MANUAL for i in items))

    def test_nonstandalone_and_disabled_stay_manual(self):
        for enabled, standalone in ((False, True), (True, False)):
            settings = self.settings()
            settings["enabled"] = enabled
            with patch.dict(CONFIG, autoloader=settings):
                cells, items, *_ = convert_text(SOURCE, "Bronze_Layer.py", "python", standalone=standalone)
            self.assertIn('format("cloudFiles")', code(cells))
            self.assertFalse(any(i.rule_id == "X1-WAIT" for i in items))

    def test_reader_with_parentheses_and_reverse_option_order(self):
        source = SOURCE.replace('df = spark.readStream.format("cloudFiles")\\\n', 'df = (spark.readStream.format("cloudFiles")\n')
        source = source.replace('.load(f"abfss://source@account.dfs.core.windows.net/{p_file_name}*.parquet")',
                                '.load(f"abfss://source@account.dfs.core.windows.net/{p_file_name}*.parquet"))')
        with patch.dict(CONFIG, autoloader=self.settings()):
            cells, items, *_ = convert(source)
        self.assertIn(".schema(", code(cells))
        self.assertTrue(any(i.rule_id == "X1-READER" for i in items))

    def test_multiple_readers_fail_atomically_and_name_collisions_are_blocked(self):
        second = SOURCE.split("# COMMAND ----------", 1)[1]
        second = second.replace("df", "other_df").replace('cloudFiles.format", "parquet"', 'cloudFiles.format", "json"')
        settings = self.settings()
        settings["schemas"]["Bronze_Layer.py"]["other_df"] = "id STRING"
        for source in (SOURCE + "\n# COMMAND ----------\n" + second,
                       SOURCE.replace("p_file_name =", "_a2f_query_1_1 = None\np_file_name =")):
            with self.subTest(source=source), patch.dict(CONFIG, autoloader=settings):
                cells, items, *_ = convert(source)
                self.assertNotIn(".awaitTermination()", code(cells))
                self.assertFalse(any(i.rule_id == "X1-READER" for i in items))

    def test_secret_configured_mapping_and_placeholder_dependency(self):
        text = '# Databricks notebook source\nk = dbutils.secrets.get(scope="scope", key="key")'
        for mapping, expected in (({}, REVIEW),
                                  ({"scope": "https://your-vault.vault.azure.net/"}, REVIEW),
                                  ({"scope": "https://configured-vault.vault.azure.net/"}, AUTO)):
            with self.subTest(mapping=mapping), patch.dict(CONFIG, secret_scopes=mapping):
                cells, items, ctx, *_ = convert_text(text, "secret.py", "python")
                self.assertEqual([i.level for i in items], [expected])
                self.assertIn("keyvault", ctx.checklist)
                if expected == AUTO:
                    self.assertIn("notebookutils.credentials.getSecret('https://configured-vault.vault.azure.net/', 'key')", code(cells))
                else:
                    self.assertIn("notebookutils.credentials.getSecret(", code(cells))
                    self.assertNotIn("dbutils.secrets.get", code(cells))
                    self.assertIn("REVIEW[ADB2FABRIC] S1", code(cells))
                    self.assertIn("vault URL remains a migration dependency", items[0].message)
                    self.assertNotIn("TODO[ADB2FABRIC]", code(cells))

    def test_multiline_secret_solution_executes_equivalent_fabric_call(self):
        source = '''# Databricks notebook source
storage_account_access_key = dbutils.secrets.get(
    scope="example-scope",
    key="storage-account-key"
)
print("after secret")
'''
        calls = []

        class FakeCredentials:
            def getSecret(self, *args):
                calls.append(args)
                return "fake-test-value"

        class FakeUtils:
            credentials = FakeCredentials()

        for mapping, expected_url, expected_level in (
            ({"example-scope": "https://your-vault.vault.azure.net/"}, "https://your-vault.vault.azure.net/", REVIEW),
            ({"example-scope": "https://configured-vault.vault.azure.net/"}, "https://configured-vault.vault.azure.net/", AUTO),
        ):
            with self.subTest(mapping=mapping), patch.dict(CONFIG, secret_scopes=mapping):
                cells, items, ctx, *_ = convert_text(source, "Bronze_Layer.py", "python")
                converted = code(cells)
                ast.parse(converted)
                namespace = {"notebookutils": FakeUtils(), "print": lambda _: None}
                exec(converted, namespace)
                self.assertEqual(namespace["storage_account_access_key"], "fake-test-value")
                self.assertEqual(calls[-1], (expected_url, "storage-account-key"))
                self.assertEqual([(i.rule_id, i.level) for i in items], [("S1", expected_level)])
                self.assertEqual(items[0].line, 2)
                self.assertIn("notebookutils.credentials.getSecret", items[0].converted)
                self.assertNotIn("dbutils.secrets.get", converted)

    def test_secret_string_and_comment_not_rewritten(self):
        source = '''# Databricks notebook source
# dbutils.secrets.get("scope", "key")
example = 'dbutils.secrets.get("scope", "key")'
'''
        cells, items, *_ = convert_text(source, "strings.py", "python")
        self.assertIn('example = \'dbutils.secrets.get("scope", "key")\'', code(cells))
        self.assertFalse(any(i.rule_id == "S1" for i in items))

    def test_reversed_keywords_nested_key_and_unicode_offsets(self):
        source = '''# Databricks notebook source
label = "caf\u00e9"; a = dbutils.secrets.get(key=lookup("key"), scope="scope"); b = dbutils.secrets.get("scope", "other")
'''
        with patch.dict(CONFIG, secret_scopes={"scope": "https://configured-vault.vault.azure.net/"}):
            cells, items, *_ = convert_text(source, "unicode.py", "python")
        converted = code(cells)
        ast.parse(converted)
        self.assertEqual(converted.count("notebookutils.credentials.getSecret("), 2)
        self.assertIn("lookup('key')", converted)
        self.assertEqual([i.level for i in items], [AUTO, AUTO])

    def test_nested_secret_lookups_stay_manual_without_partial_rewrite(self):
        source = '# Databricks notebook source\nk = dbutils.secrets.get("scope", dbutils.secrets.get("scope", "other"))'
        cells, items, *_ = convert_text(source, "nested.py", "python")
        self.assertNotIn("notebookutils.credentials.getSecret", code(cells))
        self.assertTrue(items)
        self.assertTrue(all(i.level == MANUAL for i in items))

    def test_dynamic_scope_never_inferred(self):
        with patch.dict(CONFIG, secret_scopes={"scope": "https://configured-vault.vault.azure.net/"}):
            cells, items, *_ = convert_text('# Databricks notebook source\nk = dbutils.secrets.get(scope, key)',
                                           "t.py", "python")
        self.assertEqual(items[0].level, MANUAL)
        self.assertIn("dbutils.secrets.get", code(cells))

    def test_secret_unknown_or_duplicate_arguments_stay_manual(self):
        for call in ('dbutils.secrets.get("scope", "key", extra="value")',
                     'dbutils.secrets.get("scope", scope="key")',
                     'dbutils.secrets.get(scope="scope", key="key", key="other")'):
            with self.subTest(call=call), patch.dict(CONFIG, secret_scopes={"scope": "https://configured-vault.vault.azure.net/"}):
                cells, items, *_ = convert_text("# Databricks notebook source\nk = " + call, "t.py", "python")
                self.assertEqual(items[0].level, MANUAL)
                self.assertNotIn("notebookutils.credentials.getSecret", code(cells))

    def test_configured_widget_default_and_parameter_tag(self):
        with patch.dict(CONFIG, parameter_defaults={"file_name": "products"}):
            cells, items, ctx, *_ = convert()
            notebook = build_notebook("Bronze_Layer.py", cells, ctx, items, "python")
        self.assertEqual(ctx.params, {"file_name": "'products'"})
        self.assertEqual(next(i.level for i in items if i.rule_id == "W2"), AUTO)
        parameter_cell = next(c for c in notebook["cells"] if c.get("id") == "a2f-parameters")
        self.assertEqual(parameter_cell["metadata"]["tags"], ["parameters"])
        self.assertIn("p_file_name = file_name", code(cells))
        self.assertIn("parameters", ctx.checklist)

    def test_config_validation(self):
        changes = [
            ("secret_scopes", {"x": "http://bad.vault.azure.net/"}),
            ("secret_scopes", {"x": "https://vault.vault.azure.net/secrets/key"}),
            ("secret_scopes", {"x": "https://user:pass@vault.vault.azure.net/"}),
            ("parameter_defaults", {"x": 1}),
            ("parameter_defaults", {"class": "products"}),
            ("autoloader", {"enabled": "true", "schemas": {}}),
            ("autoloader", {"enabled": True, "schemas": {}, "schema_mode": "guess"}),
            ("autoloader", {"enabled": True, "schemas": {}, "remove_checkpoint_resets": "true"}),
            ("autoloader", {"enabled": True, "schemas": {"../x.py": {"df": "id INT"}}}),
            ("autoloader", {"enabled": True, "schemas": {"x.py": {"df": ""}}}),
            ("autoloader", {"enabled": True, "schemas": {"x.py": {"df": ["id", "INT"]}}}),
        ]
        for section, value in changes:
            data = copy.deepcopy(CONFIG)
            data[section] = value
            with self.subTest(section=section, value=value), self.assertRaises(ConfigError):
                validate_config(data)
        validate_config(CONFIG)


if __name__ == "__main__":
    unittest.main()
