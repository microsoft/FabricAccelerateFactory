import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, DEFAULT_CONFIG, validate_config  # noqa: E402
from adb2fabric.converter import convert_text  # noqa: E402
from adb2fabric.paths import map_paths  # noqa: E402
from adb2fabric.model import NotebookResult  # noqa: E402
from adb2fabric.report import totals  # noqa: E402


class PathMappings(unittest.TestCase):
    def mappings(self, source="dbfs:/checkpoints", target="Files/checkpoints", **extra):
        return [{"id": "checkpoints", "source": source, "target": target, **extra}]

    def convert(self, body, mappings=None, language="python"):
        header = "--" if language == "sql" else "#"
        with patch.dict(CONFIG, path_mappings=self.mappings() if mappings is None else mappings):
            return convert_text(f"{header} Databricks notebook source\n{body}", "paths.py", language)

    def test_screenshot_checkpoint_fstrings_map_both_paths(self):
        body = (
            'dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=True)\n'
            'dbutils.fs.rm(f"dbfs:/checkpoints/schema/{p_file_name}", recurse=True)\n'
        )
        cells, items, ctx, cl, ml, _ = self.convert(body)
        code = "\n".join(cells[0][2])
        self.assertIn('notebookutils.fs.rm(f"Files/checkpoints/write/{p_file_name}", recurse=True)', code)
        self.assertIn('notebookutils.fs.rm(f"Files/checkpoints/schema/{p_file_name}", recurse=True)', code)
        self.assertNotIn("TODO", code)
        self.assertNotIn("REVIEW", code)
        self.assertNotIn("dbfs:", code)
        self.assertEqual((cl, ml), (2, 0))
        self.assertEqual([i.rule_id for i in items].count("P-MAP"), 2)
        self.assertNotIn("P1", [i.rule_id for i in items])
        self.assertIn("storage", ctx.checklist)
        self.assertIn("lakehouse", ctx.checklist)
        self.assertEqual({i.level for i in items if i.rule_id == "P-MAP"}, {"AUTO"})
        self.assertTrue(all(i.converted.startswith("notebookutils.fs.rm") for i in items if i.rule_id == "P-MAP"))
        ast.parse(code)

    def test_abfss_destination_and_literal_variable_assignment(self):
        target = "abfss://workspace@onelake.dfs.fabric.microsoft.com/lakehouse.Lakehouse/Files/checkpoints"
        cells, items, ctx, *_ = self.convert('CHECKPOINT_ROOT = "dbfs:/checkpoints"\np = CHECKPOINT_ROOT + "/write"\n',
                                           self.mappings(target=target))
        self.assertIn(f'CHECKPOINT_ROOT = "{target}"', "\n".join(cells[0][2]))
        self.assertNotIn("lakehouse", ctx.checklist)
        self.assertEqual([i.rule_id for i in items], ["P-MAP"])

    def test_longest_prefix_wins_and_replacements_do_not_cascade(self):
        mappings = self.mappings("dbfs:/checkpoints", "Files/general")
        mappings += [{"id": "specific", "source": "dbfs:/checkpoints/write", "target": "Files/special"}]
        cells, items, *_ = self.convert('x = "dbfs:/checkpoints/write/a"\ny = "dbfs:/checkpoints/schema/b"', mappings)
        code = "\n".join(cells[0][2])
        self.assertIn('"Files/special/a"', code)
        self.assertIn('"Files/general/schema/b"', code)
        self.assertIn("specific:", items[0].message)

    def test_path_boundaries_and_partial_mapping_keep_manual_flags(self):
        cells, items, _, _, ml, _ = self.convert(
            'x = ["dbfs:/checkpoints/a", "dbfs:/checkpoints_backup/a", "dbfs:/other/b"]\n'
        )
        code = "\n".join(cells[0][2])
        self.assertIn('"Files/checkpoints/a"', code)
        self.assertIn('"dbfs:/checkpoints_backup/a"', code)
        self.assertIn('"dbfs:/other/b"', code)
        self.assertIn("TODO", code)
        self.assertEqual(ml, 1)
        self.assertIn("P1", [i.rule_id for i in items])

    def test_disabled_empty_and_omitted_mappings_retain_manual_behaviour(self):
        for mappings in ([], self.mappings(enabled=False)):
            with self.subTest(mappings=mappings):
                cells, items, _, _, ml, _ = self.convert('x = "dbfs:/checkpoints/write/a"', mappings)
                self.assertIn("dbfs:/checkpoints", "\n".join(cells[0][2]))
                self.assertEqual(ml, 1)
                self.assertEqual([i.rule_id for i in items], ["P1"])
        config = copy.deepcopy(CONFIG)
        config.pop("path_mappings", None)
        validate_config(config)

    def test_python_and_sql_comments_and_markdown_are_unchanged(self):
        with patch.dict(CONFIG, path_mappings=self.mappings()):
            for kind, lines in (
                ("code", ['# "dbfs:/checkpoints/a"', 'x = 1  # "dbfs:/checkpoints/b"']),
                ("sql", ["-- 'dbfs:/checkpoints/a'", "/* 'dbfs:/checkpoints/b' */ SELECT 1"]),
                ("other", ['"dbfs:/checkpoints/a"']),
            ):
                with self.subTest(kind=kind):
                    rewritten, evidence = map_paths(lines, kind)
                    self.assertEqual(rewritten, lines)
                    self.assertEqual(evidence, {})
        cells, items, *_ = self.convert("# MAGIC %md\n# MAGIC dbfs:/checkpoints/a\n")
        self.assertEqual(cells[0][2], ["dbfs:/checkpoints/a"])
        self.assertNotIn("P-MAP", [i.rule_id for i in items])

    def test_sql_literals_backticks_and_embedded_multiline_sql(self):
        cells, items, _, _, ml, _ = self.convert(
            "SELECT * FROM delta.`dbfs:/checkpoints/write/a` WHERE p = 'dbfs:/checkpoints/schema/a'",
            language="sql",
        )
        self.assertIn("delta.`Files/checkpoints/write/a`", "\n".join(cells[0][2]))
        self.assertEqual([i.rule_id for i in items], ["P-MAP", "P-MAP"])
        self.assertEqual(ml, 0)
        cells, items, *_ = self.convert(
            'q = """\nSELECT * FROM delta.`dbfs:/checkpoints/write/a`\n"""\n'
        )
        code = "\n".join(cells[0][2])
        self.assertIn("delta.`Files/checkpoints/write/a`", code)
        self.assertTrue(code.startswith('q = """'))
        self.assertNotIn("REVIEW", code.split('"""')[1])
        self.assertEqual(items[0].line, 3)
        ast.parse(code)

    def test_raw_and_single_quote_strings_and_trailing_slashes(self):
        cells, items, *_ = self.convert(
            "x = r'dbfs:/checkpoints/a'\ny = 'dbfs:/checkpoints/'",
            self.mappings(source="dbfs:/checkpoints/", target="Files/checkpoints/"),
        )
        code = "\n".join(cells[0][2])
        self.assertIn("r'Files/checkpoints/a'", code)
        self.assertIn("'Files/checkpoints/'", code)
        self.assertEqual(len(items), 2)
        ast.parse(code)

    def test_unhandled_dbutils_remains_manual_after_path_mapping(self):
        _, items, _, _, ml, _ = self.convert('dbutils.unknown("dbfs:/checkpoints/a")')
        self.assertIn("Z9", [i.rule_id for i in items])
        self.assertEqual(ml, 1)

    def test_mapping_does_not_remove_other_manual_constructs(self):
        _, items, _, _, ml, _ = self.convert('df = spark.readStream.format("cloudFiles").load("dbfs:/checkpoints/a")')
        self.assertIn("P-MAP", [i.rule_id for i in items])
        self.assertIn("X1", [i.rule_id for i in items])
        self.assertEqual(ml, 1)

    def test_screenshot_streaming_chains_convert_paths_without_hiding_autoloader(self):
        body = (
            'df = spark.readStream.format("cloudFiles")\\\n'
            '    .option("cloudFiles.format", "parquet")\\\n'
            '    .option("cloudFiles.schemaLocation", f"dbfs:/checkpoints/schema/{p_file_name}")\\\n'
            '    .load(f"abfss://source@{storage_account_name}.dfs.core.windows.net/{p_file_name}*.parquet")\n'
            '\n'
            'df.writeStream.format("delta")\\\n'
            '    .outputMode("append")\\\n'
            '    .option("checkpointLocation", f"dbfs:/checkpoints/write/{p_file_name}")\\\n'
            '    .trigger(availableNow=True)\\\n'
            '    .toTable(f"bronze.{p_file_name}")\n'
        )
        with patch.dict(CONFIG, autoloader={"enabled": True, "schemas": {}, "schema_mode": "explicit"}):
            cells, items, _, _, ml, _ = self.convert(body)
        code = "\n".join(cells[0][2])
        self.assertIn('f"Files/checkpoints/schema/{p_file_name}"', code)
        self.assertIn('f"Files/checkpoints/write/{p_file_name}"', code)
        self.assertNotIn("dbfs:/", code)
        self.assertNotIn("P1", [i.rule_id for i in items])
        self.assertEqual(sum(i.rule_id == "P-MAP" and i.level == "AUTO" for i in items), 2)
        self.assertEqual(sum(i.rule_id == "X1" and i.level == "MANUAL" for i in items), 3)
        self.assertEqual(ml, 3)
        ast.parse(code)

    def test_successful_mapping_changes_completion_and_coverage_not_just_label(self):
        body = 'dbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=True)'
        scores = []
        for mappings in (self.mappings(enabled=False), self.mappings(enabled=True)):
            cells, items, ctx, cl, ml, warnings = self.convert(body, mappings)
            result = NotebookResult("paths.py", "paths.ipynb", "python", len(cells),
                                    cl, ml, items, {}, [], warnings)
            scores.append(totals([result]))
        before, after = scores
        self.assertEqual((before["manual"], before["manual_items"]), (1, 1))
        self.assertEqual((after["converted"], after["auto"]), (1, 2))
        self.assertEqual(after["manual_items"], 0)
        self.assertEqual(after["review_items"], 0)
        self.assertEqual(after["notebook_completion"], 1)
        self.assertEqual(after["strict_coverage"], 1)
        self.assertEqual(after["weighted_coverage"], 1)
        self.assertGreater(after["weighted_coverage"], before["weighted_coverage"])

    def test_dynamic_paths_are_not_guessed(self):
        for body, mappings in (
            ('p = f"{root}/checkpoints/{name}"', self.mappings()),
            ('p = f"{root}/mnt/raw/{name}"', self.mappings("/mnt/raw", "Files/raw")),
            ('p = f"dbfs:/checkpoints{suffix}"', self.mappings()),
        ):
            with self.subTest(body=body):
                cells, items, *_ = self.convert(body, mappings)
                self.assertNotIn("P-MAP", [i.rule_id for i in items])
                self.assertTrue(cells[0][2][0].startswith(body))

    def test_dbfs_root_mapping_and_multiple_fstring_variables(self):
        body = 'p = f"dbfs:/checkpoints/{folder}/{file_name}"'
        cells, items, *_ = self.convert(body, self.mappings("dbfs:/", "Files/dbfs"))
        self.assertIn('f"Files/dbfs/checkpoints/{folder}/{file_name}"', cells[0][2][0])
        self.assertEqual([i.rule_id for i in items], ["P-MAP"])

    def test_invalid_mappings_are_rejected(self):
        invalid = [
            "not a list",
            [{"source": "dbfs:/a", "target": "Files/a"}],
            self.mappings(target="dbfs:/still_legacy"),
            self.mappings(target='Files/unsafe"quote'),
            self.mappings(target="Files/{variable}"),
            self.mappings(target="Files/../unsafe"),
            self.mappings(target="Files/a\nb"),
            self.mappings(target="abfss://<workspace>@host/<lakehouse>/Files/a"),
            self.mappings(enabled="yes"),
            self.mappings(source="relative/path"),
            self.mappings() + self.mappings(),
            self.mappings() + [{"id": "another", "source": "dbfs:/checkpoints/", "target": "Files/other"}],
        ]
        for mappings in invalid:
            with self.subTest(mappings=mappings):
                config = copy.deepcopy(CONFIG)
                config["path_mappings"] = mappings
                with self.assertRaises(ConfigError):
                    validate_config(config)

    def test_active_json_mapping_reaches_notebooks_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "adb2fabric", root / "adb2fabric", ignore=shutil.ignore_patterns("__pycache__"))
            config = copy.deepcopy(CONFIG)
            config["path_mappings"] = self.mappings()
            (root / "adb2fabric" / DEFAULT_CONFIG.name).write_text(json.dumps(config), encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "checkpoint.py").write_text(
                '# Databricks notebook source\ndbutils.fs.rm(f"dbfs:/checkpoints/write/{p_file_name}", recurse=True)\n',
                encoding="utf-8",
            )
            out = root / "output"
            proc = subprocess.run(
                [sys.executable, "-m", "adb2fabric", str(source), "-o", str(out), "--no-open"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = next(out.parent.glob(out.name + "_*"))
            nb = json.loads((out / "checkpoint.ipynb").read_text(encoding="utf-8"))
            self.assertIn('f"Files/checkpoints/write/{p_file_name}"', "".join(nb["cells"][1]["source"]))
            report = json.loads((out / "_report" / "report.json").read_text(encoding="utf-8"))
            result = report["notebooks"][0]
            self.assertEqual(result["status"], "Converted")
            self.assertEqual(result["manual_lines"], 0)
            self.assertEqual(report["totals"]["manual_items"], 0)
            self.assertEqual(report["totals"]["review_items"], 0)
            self.assertEqual(report["totals"]["auto"], 2)
            self.assertEqual(report["totals"]["strict_coverage"], 1)
            self.assertEqual(report["totals"]["weighted_coverage"], 1)
            self.assertEqual(report["totals"]["notebook_completion"], 1)
            self.assertEqual([i["rule_id"] for i in result["items"]], ["P-MAP", "F1"])
            self.assertIn("Configured ADB", (out / "_report" / "report.html").read_text(encoding="utf-8"))
            self.assertIn("checkpoints: dbfs:/checkpoints", (out / "_report" / "items.csv").read_text(encoding="utf-8-sig"))
            manual_csv = (out / "_report" / "manual_work.csv").read_text(encoding="utf-8-sig")
            self.assertEqual(len(manual_csv.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
