import ast
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, validate_config
from adb2fabric.converter import convert_file, convert_text, build_notebook
from adb2fabric.model import NotebookResult
from adb2fabric.report import totals

HEADER = "# Databricks notebook source\n"
SET = 'dbutils.jobs.taskValues.set(key="output_data_sets", value=["orders", "customer", "products"])'


class TaskValues(unittest.TestCase):
    def test_parameters_is_auto_and_emits_expected_output(self):
        cells, items, ctx, cl, ml, _ = convert_text(HEADER + SET, "parameters.py", "python")
        code = "\n".join("\n".join(lines) for _, kind, lines in cells if kind == "code")
        ast.parse(code)
        self.assertIn("fabric_task_values['output_data_sets'] =", code)
        self.assertNotIn("dbutils", code)
        self.assertNotIn("TODO", code)
        self.assertEqual([(i.rule_id, i.level) for i in items], [("J1", "AUTO")])
        self.assertEqual(items[0].line, 2)
        self.assertEqual((cl, ml), (1, 0))
        outputs = []
        exec(code, {"notebookutils": SimpleNamespace(notebook=SimpleNamespace(exit=outputs.append))})
        self.assertEqual(json.loads(outputs[0]), {"output_data_sets": ["orders", "customer", "products"]})
        result = NotebookResult("parameters.py", "parameters.ipynb", "python", len(cells),
                                cl, ml, items, {}, [], [])
        self.assertEqual(totals([result])["notebook_completion"], 1)
        self.assertEqual(totals([result])["strict_coverage"], 1)
        nb = build_notebook("parameters.py", cells, ctx, items, "python")
        self.assertIn("orchestration", "".join(nb["cells"][0]["source"]))
        self.assertEqual(len({c["id"] for c in nb["cells"]}), len(nb["cells"]))

    def test_multiple_calls_finalize_once_after_later_processing(self):
        body = SET + '\n# COMMAND ----------\nx = 7\n' + (
            'dbutils.jobs.taskValues.set(\n    value={"count": [1, True, None]},\n    key="stats"\n)\n'
            'completed = True\n'
        )
        cells, items, *_ = convert_text(HEADER + body, "main.py", "python")
        code = "\n".join("\n".join(lines) for _, kind, lines in cells)
        state = {}
        outputs = []
        state["notebookutils"] = SimpleNamespace(notebook=SimpleNamespace(exit=outputs.append))
        exec(code, state)
        self.assertTrue(state["completed"])
        self.assertEqual(state["x"], 7)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(json.loads(outputs[0])["stats"], {"count": [1, True, None]})
        self.assertEqual(sum(i.level == "AUTO" and i.rule_id == "J1" for i in items), 2)

    def test_unsafe_forms_remain_manual_without_exit(self):
        bodies = [
            'dbutils.jobs.taskValues.set(key="k", value=variable)',
            'dbutils.jobs.taskValues.set("k", [1])',
            'dbutils.jobs.taskValues.get(taskKey="t", key="k")',
            'x = dbutils.jobs.taskValues.set(key="k", value=[1])',
            'if True:\n    ' + SET,
            'def helper():\n    ' + SET,
            SET + '\ndbutils.notebook.exit("existing")',
            SET + '\nnotebookutils.notebook.exit("existing")',
            SET + '\nfabric_task_values = {}',
            SET + '\nimport json as _a2f_json',
            SET + '\n# COMMAND ----------\n# MAGIC %sql\n# MAGIC SELECT 1',
            SET + '; print("same line")',
            'dbutils.jobs.taskValues.set(key="k", value=(1, 2))',
            'dbutils.jobs.taskValues.set(key="k", value={1: "not-string-key"})',
            SET + '\n# COMMAND ----------\n' + SET + '; print("unsafe")',
        ]
        for body in bodies:
            with self.subTest(body=body):
                cells, items, *_ = convert_text(HEADER + body, "main.py", "python")
                code = "\n".join("\n".join(lines) for _, kind, lines in cells)
                self.assertNotIn("_a2f_json.dumps", code)
                self.assertTrue(any(i.rule_id == "J1" and i.level == "MANUAL" for i in items))
                self.assertFalse(any(i.rule_id == "J1" and i.level == "AUTO" for i in items))

    def test_plain_module_and_referenced_notebooks_are_not_finalized(self):
        for header, standalone in (("", True), (HEADER, False)):
            cells, items, *_ = convert_text(header + SET, "parameters.py", "python", standalone=standalone)
            self.assertNotIn("_a2f_json.dumps", str(cells))
            self.assertEqual(items[0].level, "MANUAL")
        for reference in ("import parameters", "from parameters import value",
                          "# MAGIC %run ./parameters", "# MAGIC %run ./parameters.py"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(reference=reference):
                root = Path(tmp)
                source = root / "parameters.py"
                source.write_text(HEADER + SET, encoding="utf-8")
                (root / "caller.py").write_text(HEADER + reference, encoding="utf-8")
                result = convert_file(source, root, root / "out", write=False)
                self.assertEqual(result.status, "Needs manual work")
                self.assertEqual(result.count("AUTO"), 0)

    def test_feature_can_be_disabled_and_config_validated(self):
        settings = dict(CONFIG["task_values"], enabled=False)
        with patch.dict(CONFIG, task_values=settings):
            _, items, *_ = convert_text(HEADER + SET, "parameters.py", "python")
            self.assertEqual(items[0].level, "MANUAL")
        config = copy.deepcopy(CONFIG)
        config["task_values"]["enabled"] = "yes"
        with self.assertRaises(ConfigError):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
