import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from syn2fabric import cli  # noqa: E402
from syn2fabric.config import DEFAULT_CONFIG, ConfigError, load_settings, validate  # noqa: E402

ENV = dict(os.environ, SYN2FABRIC_NO_OPEN="1")


def run(*args):
    return subprocess.run([sys.executable, "-m", "syn2fabric", *args], cwd=ROOT, capture_output=True, text=True, env=ENV)


def write_notebook(path, source, kernel="synapse_pyspark"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 2,
        "metadata": {"kernelspec": {"name": kernel}, "language_info": {"name": "python"}},
        "cells": [{"cell_type": "code", "metadata": {}, "source": source.splitlines(True), "outputs": [], "execution_count": None}],
    }), encoding="utf-8")


class CliOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.src = self.base / "synapse"
        write_notebook(self.src / "a" / "good.ipynb", "mssparkutils.fs.ls('Files')\n")
        write_notebook(self.src / "bad.ipynb", "df = spark.read.synapsesql('p.dbo.t')\n")
        (self.src / "pipeline.json").write_text(json.dumps({"name": "p", "properties": {}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def output_dirs(self, prefix="converted"):
        return sorted(p for p in self.base.glob(prefix + "_*") if p.is_dir())

    def test_each_run_creates_a_new_timestamped_folder_and_keeps_existing_files(self):
        keep = self.base / "converted_keep" / "precious.ipynb"
        keep.parent.mkdir()
        keep.write_text("keep", encoding="utf-8")
        for _ in range(2):
            r = run(str(self.src), "-o", str(self.base / "converted"), "--no-open")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dirs = [d for d in self.output_dirs() if re.fullmatch(r"converted_\d{12}(?:_\d{2})?", d.name)]
        self.assertEqual(len(dirs), 2)
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")
        for d in dirs:
            self.assertTrue((d / "a" / "good.ipynb").is_file())
            for name in ("report.html", "report.json", "items.csv", "manual_work.csv"):
                self.assertTrue((d / "_report" / name).is_file(), name)

    def test_dry_run_writes_report_but_no_notebooks(self):
        r = run(str(self.src), "-o", str(self.base / "converted"), "--dry-run", "--no-open")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = self.output_dirs()[-1]
        self.assertEqual(list(out.rglob("*.ipynb")), [])
        self.assertTrue((out / "_report" / "report.html").is_file())

    def test_report_contents(self):
        run(str(self.src), "-o", str(self.base / "converted"), "--no-open")
        data = json.loads((self.output_dirs()[-1] / "_report" / "report.json").read_text(encoding="utf-8"))
        statuses = {n["source"]: n["status"] for n in data["notebooks"]}
        self.assertEqual(statuses, {"a/good.ipynb": "Converted", "bad.ipynb": "Needs manual work"})
        self.assertEqual([s["file"] for s in data["skipped"]], ["pipeline.json"])
        self.assertEqual(data["totals"]["notebooks"], 2)

    def test_output_inside_input_is_rejected(self):
        r = run(str(self.src), "-o", str(self.src / "out"), "--no-open")
        self.assertEqual(r.returncode, 2)
        self.assertIn("overlaps", r.stderr)

    def test_output_prefix_never_reuses_or_deletes_existing_folders(self):
        existing = self.base / "converted_keep"
        write_notebook(existing / "unrelated.ipynb", "print('mine')\n")
        r = run(str(self.src), "-o", str(existing), "--no-open")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((existing / "unrelated.ipynb").is_file())
        self.assertEqual(len([d for d in self.base.glob("converted_keep_*") if d.is_dir()]), 1)

    def test_default_output_is_a_sibling_timestamped_folder(self):
        r = run(str(self.src), "--no-open")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.output_dirs("synapse_fabric")), 1)

    def test_fail_below_and_missing_input(self):
        self.assertEqual(run(str(self.src), "-o", str(self.base / "c"), "--no-open", "--fail-below", "99").returncode, 3)
        self.assertEqual(run(str(self.base / "missing")).returncode, 2)

    def test_no_notebooks_found(self):
        empty = self.base / "empty"
        empty.mkdir()
        r = run(str(empty), "--no-open")
        self.assertEqual(r.returncode, 1)

    def test_custom_rules_file_and_invalid_rules_file(self):
        data = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        write_notebook(self.src / "kv.ipynb", 'x = mssparkutils.credentials.getSecret("kv1", "s")\n')
        data["key_vaults"] = {"kv1": "https://custom.vault.azure.net/"}
        rules = self.base / "my-rules.json"
        rules.write_text(json.dumps(data), encoding="utf-8")
        r = run(str(self.src), "-o", str(self.base / "custom"), "--no-open", "--config", str(rules))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        converted = next(self.base.glob("custom_*")) / "kv.ipynb"
        self.assertIn("https://custom.vault.azure.net/", converted.read_text(encoding="utf-8"))
        rules.write_text("{broken", encoding="utf-8")
        r = run(str(self.src), "--no-open", "--config", str(rules))
        self.assertEqual(r.returncode, 2)
        self.assertIn("rules file", r.stderr)

    def test_json_artifact_next_to_ipynb_does_not_collide(self):
        write_notebook(self.src / "same.ipynb", "print(1)\n")
        artifact = {"name": "same", "properties": {"bigDataPool": {"referenceName": "p"}, "cells": [
            {"cell_type": "code", "metadata": {}, "source": ["print(2)"]}]}}
        (self.src / "same.json").write_text(json.dumps(artifact), encoding="utf-8")
        run(str(self.src), "-o", str(self.base / "converted"), "--no-open")
        out = self.output_dirs()[-1]
        self.assertTrue((out / "same.ipynb").is_file())
        self.assertTrue((out / "same_json.ipynb").is_file())

    def test_discover_skips_hidden_and_converted_folders(self):
        write_notebook(self.src / ".git" / "x.ipynb", "print(1)\n")
        write_notebook(self.src / "old_fabric" / "y.ipynb", "print(1)\n")
        found, _ = cli.discover(self.src, False)
        self.assertEqual(sorted(p.name for p, _ in found), ["bad.ipynb", "good.ipynb"])


class ConfigValidation(unittest.TestCase):
    def data(self):
        return copy.deepcopy(json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8")))

    def test_bundled_rules_are_valid_and_portable(self):
        cfg = load_settings()
        self.assertTrue(cfg.rules)
        for section in ("key_vaults", "linked_services", "notebook_mappings", "table_mappings"):
            self.assertEqual(cfg.data[section], {}, section)
        self.assertEqual(cfg.data["path_mappings"], [])
        text = DEFAULT_CONFIG.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)password\s*[:=]|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def test_invalid_configurations_are_rejected(self):
        def mutate(fn):
            d = self.data()
            fn(d)
            return d
        bad = {
            "duplicate rule id": mutate(lambda d: d["rules"].append(copy.deepcopy(d["rules"][0]))),
            "bad regex": mutate(lambda d: d["rules"][0].update(pattern="(")),
            "manual with replacement": mutate(lambda d: d["rules"].append(
                {"id": "X", "title": "x", "pattern": "x", "level": "MANUAL", "replacement": "y"})),
            "unknown handler": mutate(lambda d: d["rules"][0].update(handler="nope")),
            "missing handler groups": mutate(lambda d: d["rules"].append(
                {"id": "X", "title": "x", "pattern": "x", "level": "REVIEW", "handler": "run_notebook"})),
            "unknown checklist key": mutate(lambda d: d["rules"][0].update(checklist=["nope", "x"])),
            "bad table mapping": mutate(lambda d: d.update(table_mappings={"onepart": "x.y"})),
            "bad vault url": mutate(lambda d: d.update(key_vaults={"kv": "not-a-url"})),
            "unknown key": mutate(lambda d: d.update(surprise=1)),
            "unknown magic kind": mutate(lambda d: d["magic_commands"][0].update(kind="nope")),
        }
        for name, data in bad.items():
            with self.assertRaises(ConfigError, msg=name):
                validate(data)

    def test_replacement_and_handler_are_exclusive(self):
        d = self.data()
        d["rules"][1].update(replacement="x")
        with self.assertRaises(ConfigError):
            validate(d)


if __name__ == "__main__":
    unittest.main()
