import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.config import CONFIG, ConfigError, DEFAULT_CONFIG, HANDLER_GROUPS, load_config  # noqa: E402
from adb2fabric.converter import build_notebook, convert_text  # noqa: E402
from adb2fabric.rules import HANDLERS, RULES, build_rules  # noqa: E402


class RuleConfiguration(unittest.TestCase):
    def test_portable_defaults(self):
        config = load_config()
        for key in ("secret_scopes", "table_mappings", "notebook_mappings"):
            self.assertEqual(config[key], {})
        self.assertEqual(config["path_mappings"], [])
        self.assertEqual(config["autoloader"]["schema_mode"], "explicit")
        self.assertFalse(config["autoloader"]["remove_checkpoint_resets"])
        self.assertEqual(config["autoloader"]["schemas"], {})

    def write_config(self, root, data):
        path = root / "rules.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_inventory_and_handler_registry(self):
        self.assertEqual(len(CONFIG["rules"]), 33)
        self.assertEqual(len(RULES), 33)
        self.assertEqual(len(CONFIG["magic_commands"]), 10)
        self.assertEqual(len(CONFIG["checklist_text"]), 13)
        self.assertEqual(set(HANDLERS), set(HANDLER_GROUPS))
        self.assertEqual(load_config(), CONFIG)

    def test_missing_and_malformed_files_do_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            for content in (None, "{", '{"schema_version":1,"schema_version":1}', "[]", "null"):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigError) as err:
                        load_config(path)
                    self.assertIn(str(path), str(err.exception))

    def test_invalid_configuration_values_are_rejected(self):
        changes = [
            ("schema_version", 2), ("schema_version", True), ("target_platform", "synapse"),
            ("rules", []), ("rules", "wrong"), ("magic_commands", []),
            ("excluded_rule_ids", ["missing"]), ("checklist_text", {}),
            ("cell_metadata", {"code": ["python"]}),
            ("cell_prefixes", {"sql": []}),
            ("notebook_metadata", {"kernel_info": {}}),
            ("fs_magic", {"api_prefix": "bad()", "commands": ["ls"], "display_result_commands": []}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for key, value in changes:
                with self.subTest(key=key, value=value):
                    data = copy.deepcopy(CONFIG)
                    data[key] = value
                    with self.assertRaises(ConfigError):
                        load_config(self.write_config(Path(tmp), data))

    def test_invalid_rule_fields_are_rejected(self):
        changes = [
            {"pattern": "["}, {"handler": "os.system"}, {"pattern": "no_named_groups"},
            {"scope": "scala"}, {"level": "SUCCESS"}, {"silent": "false"},
            {"replacement": "conflicts_with_handler"}, {"flags": ["UNKNOWN"]},
            {"checklist": ["missing", "detail"]}, {"unexpected": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for change in changes:
                with self.subTest(change=change):
                    data = copy.deepcopy(CONFIG)
                    data["rules"][0].update(change)
                    with self.assertRaises(ConfigError):
                        load_config(self.write_config(Path(tmp), data))

    def test_invalid_replacement_backreferences_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for replacement in ("\\99", "\\g<missing>", "\\q"):
                data = copy.deepcopy(CONFIG)
                data["rules"][7]["replacement"] = replacement
                with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                    load_config(self.write_config(Path(tmp), data))

    def test_duplicate_ids_and_wrong_catchall_position_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for move_catchall in (False, True):
                data = copy.deepcopy(CONFIG)
                if move_catchall:
                    data["rules"].insert(0, data["rules"].pop())
                else:
                    data["rules"][1]["id"] = data["rules"][0]["id"]
                with self.subTest(move_catchall=move_catchall), self.assertRaises(ConfigError):
                    load_config(self.write_config(Path(tmp), data))

    def test_manual_rules_cannot_silently_perform_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = copy.deepcopy(CONFIG)
            data["rules"][0]["level"] = "MANUAL"
            with self.assertRaises(ConfigError):
                load_config(self.write_config(Path(tmp), data))
            data = copy.deepcopy(CONFIG)
            data["manual_cases"]["run_with_parameters"]["level"] = "AUTO"
            with self.assertRaises(ConfigError):
                load_config(self.write_config(Path(tmp), data))

    def test_new_rules_and_empty_replacement_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = copy.deepcopy(CONFIG)
            data["rules"].insert(-1, {
                "id": "CUSTOM1", "title": "Custom mapping", "pattern": "legacy_remove\\(\\)",
                "level": "AUTO", "replacement": "", "flags": ["IGNORECASE"],
            })
            rules = build_rules(load_config(self.write_config(Path(tmp), data)))
            rule = next(r for r in rules if r.id == "CUSTOM1")
            self.assertEqual(rule.rx.sub(rule.sub, "LEGACY_REMOVE()"), "")

    def test_widget_remove_template_preserves_indentation(self):
        cells, items, *_ = convert_text(
            "# Databricks notebook source\nif True:\n    dbutils.widgets.removeAll()\n", "widgets.py", "python"
        )
        self.assertEqual(cells[0][2][1], "    pass  # [ADB2FABRIC] widgets are not used in Fabric")
        self.assertEqual([i.rule_id for i in items], ["W3"])

    def test_sample_outputs_and_findings_regression(self):
        root = ROOT / "samples" / "adb_notebooks"
        records = []
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in (".py", ".sql"):
                continue
            text = path.read_text(encoding="utf-8-sig")
            if not text.startswith(("# Databricks notebook source", "-- Databricks notebook source")):
                continue
            rel = path.relative_to(root).as_posix()
            language = "sql" if path.suffix == ".sql" else "python"
            cells, items, ctx, cl, ml, warnings = convert_text(text, rel, language)
            records.append([rel, build_notebook(rel, cells, ctx, items, language),
                            [asdict(i) for i in items], asdict(ctx), cl, ml, warnings])
        digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=lambda s: sorted(s)).encode()).hexdigest()
        # Unmapped tables are reviewed, dynamic SQL is manual, and magic findings show generated output.
        self.assertEqual(digest, "015336553e7ba1cabed004f08d06a317215438b3a4136b5fb3b044c7fbdd4331")

    def test_edits_to_package_json_drive_actual_cli_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "adb2fabric"
            shutil.copytree(ROOT / "adb2fabric", package, ignore=shutil.ignore_patterns("__pycache__"))
            data = copy.deepcopy(CONFIG)
            data["rules"][7]["replacement"] = "configured_fs.\\1"
            sql = next(m for m in data["magic_commands"] if m["kind"] == "sql")
            sql.update({"pattern": "^%query\\b", "title": "Configured SQL mapping"})
            data["excluded_rule_ids"].remove("MAG-SQL")
            data["checklist_text"]["storage"] = "Configured storage setup."
            data["cell_metadata"]["code"][0] = "configured_python"
            data["cell_prefixes"]["sql"].append("-- configured SQL prefix")
            data["notebook_metadata"]["kernelspec"]["display_name"] = "Configured Kernel"
            data["fs_magic"].update({"api_prefix": "configured_fs", "display_result_commands": []})
            data["manual_cases"]["unsupported_fs"]["title"] = "Configured unsupported fs"
            (package / DEFAULT_CONFIG.name).write_text(json.dumps(data), encoding="utf-8")
            source = root / "input"
            source.mkdir()
            (source / "test.py").write_text(
                "# Databricks notebook source\n"
                "dbutils.fs.ls('abfss://container@account/path')\n"
                "# COMMAND ----------\n# MAGIC %query\n# MAGIC SELECT 1\n"
                "# COMMAND ----------\n# MAGIC %fs ls abfss://container@account/path\n"
                "# COMMAND ----------\n# MAGIC %fs unknown path\n", encoding="utf-8"
            )
            out = root / "out"
            proc = subprocess.run(
                [sys.executable, "-m", "adb2fabric", str(source), "-o", str(out), "--no-open"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("rules config", proc.stdout)
            out = next(out.parent.glob(out.name + "_*"))
            nb = json.loads((out / "test.ipynb").read_text(encoding="utf-8"))
            self.assertEqual(nb["metadata"]["kernelspec"]["display_name"], "Configured Kernel")
            cells = nb["cells"]
            self.assertIn("Configured storage setup.", "".join(cells[0]["source"]))
            self.assertIn("configured_fs.ls", "".join(cells[1]["source"]))
            self.assertEqual(cells[1]["metadata"]["microsoft"]["language"], "configured_python")
            self.assertIn("-- configured SQL prefix", "".join(cells[2]["source"]))
            self.assertTrue("".join(cells[3]["source"]).startswith("configured_fs.ls("))
            report = json.loads((out / "_report" / "report.json").read_text(encoding="utf-8"))
            result = report["notebooks"][0]
            self.assertEqual(result["status"], "Needs manual work")
            self.assertEqual(report["totals"]["constructs"], 4)
            self.assertIn("Configured SQL mapping", [i["title"] for i in result["items"]])
            self.assertIn("Configured unsupported fs", [i["title"] for i in result["items"]])

    def test_invalid_active_config_stops_cli_before_touching_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "adb2fabric"
            shutil.copytree(ROOT / "adb2fabric", package, ignore=shutil.ignore_patterns("__pycache__"))
            (package / DEFAULT_CONFIG.name).write_text("{invalid", encoding="utf-8")
            source, out = root / "input", root / "output"
            source.mkdir()
            out.mkdir()
            notebook = out / "keep.ipynb"
            notebook.write_bytes(b"untouched")
            proc = subprocess.run(
                [sys.executable, "-m", "adb2fabric", str(source), "-o", str(out), "--no-open"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Invalid conversion configuration", proc.stderr)
            self.assertEqual(notebook.read_bytes(), b"untouched")


if __name__ == "__main__":
    unittest.main()
