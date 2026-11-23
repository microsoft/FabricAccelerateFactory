import contextlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from adb2fabric import cli


class TimestampedOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "sample.py").write_text("# Databricks notebook source\nx = 1\n", encoding="utf-8")
        self.clock = patch("adb2fabric.cli.datetime")
        clock = self.clock.start()
        clock.now.return_value = datetime(2026, 11, 23, 17, 59)
        self.addCleanup(self.clock.stop)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([str(self.source), *map(str, args), "--no-open"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_and_explicit_prefix_preserve_previous_outputs(self):
        previous = self.root / "source_fabric"
        previous.mkdir()
        sentinel = previous / "nested" / "keep.ipynb"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"keep")
        existing_run = self.root / "source_fabric_202611231759"
        existing_run.mkdir()
        (existing_run / "keep.ipynb").write_bytes(b"existing run")

        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        actual = self.root / "source_fabric_202611231759_01"
        self.assertTrue((actual / "sample.ipynb").is_file())
        self.assertIn(str(actual), stdout)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual((existing_run / "keep.ipynb").read_bytes(), b"existing run")

        base = self.root / "custom"
        base.mkdir()
        (base / "keep.ipynb").write_bytes(b"custom")
        code, stdout, stderr = self.run_cli("-o", base)
        self.assertEqual(code, 0, stderr)
        actual = self.root / "custom_202611231759"
        self.assertTrue((actual / "sample.ipynb").is_file())
        self.assertIn(str(actual), stdout)
        self.assertEqual((base / "keep.ipynb").read_bytes(), b"custom")

    def test_dry_runs_and_real_runs_never_reuse_reports(self):
        base = self.root / "output"
        code, _, stderr = self.run_cli("-o", base, "--dry-run")
        self.assertEqual(code, 0, stderr)
        dry = self.root / "output_202611231759"
        dry_report = dry / "_report" / "report.json"
        report_before = dry_report.read_bytes()
        self.assertFalse(list(dry.rglob("*.ipynb")))
        code, _, stderr = self.run_cli("-o", base)
        self.assertEqual(code, 0, stderr)
        real = self.root / "output_202611231759_01"
        self.assertTrue((real / "sample.ipynb").is_file())
        self.assertEqual(dry_report.read_bytes(), report_before)
        self.assertEqual(json.loads((real / "_report" / "report.json").read_text(encoding="utf-8"))["output"], str(real))
        code, _, stderr = self.run_cli("-o", base, "--dry-run")
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.root / "output_202611231759_02" / "_report" / "report.json").is_file())
        self.assertTrue((real / "sample.ipynb").is_file())

    def test_files_and_concurrent_reservations_cannot_be_overwritten(self):
        base = self.root / "output"
        collision = self.root / "output_202611231759"
        collision.write_bytes(b"existing file")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: cli.create_run_directory(base, self.source), range(12)))
        self.assertEqual(len(set(results)), 12)
        self.assertEqual(collision.read_bytes(), b"existing file")
        self.assertEqual({p.name for p in results}, {f"output_202611231759_{i:02}" for i in range(1, 13)})

    def test_rejects_output_within_input_and_ancestor_of_input(self):
        code, _, stderr = self.run_cli("-o", self.source / "output")
        self.assertEqual(code, 2)
        self.assertIn("overlaps the input folder", stderr)
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["sample.py"])

        ancestor = self.root / "ancestor_202611231759"
        nested_source = ancestor / "nested"
        nested_source.mkdir(parents=True)
        (nested_source / "sample.py").write_text("# Databricks notebook source\nx = 1\n", encoding="utf-8")
        self.source = nested_source
        code, _, stderr = self.run_cli("-o", self.root / "ancestor")
        self.assertEqual(code, 2)
        self.assertIn("overlaps the input folder", stderr)

    def test_unusable_parent_is_reported_without_retrying(self):
        parent = self.root / "file"
        parent.write_bytes(b"keep")
        code, _, stderr = self.run_cli("-o", parent / "output")
        self.assertEqual(code, 2)
        self.assertIn("cannot create output folder", stderr)
        self.assertEqual(parent.read_bytes(), b"keep")

    def test_no_input_notebooks_creates_no_output(self):
        (self.source / "sample.py").unlink()
        code, _, _ = self.run_cli("-o", self.root / "out")
        self.assertEqual(code, 1)
        self.assertFalse(list(self.root.glob("out_*")))


if __name__ == "__main__":
    unittest.main()
