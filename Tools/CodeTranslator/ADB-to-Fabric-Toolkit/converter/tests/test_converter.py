import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adb2fabric.converter import convert_text  # noqa: E402
from adb2fabric.model import AUTO, MANUAL, REVIEW, Item, NotebookResult  # noqa: E402
from adb2fabric.parser import parse_export  # noqa: E402
from adb2fabric.report import totals, write_reports  # noqa: E402

SAMPLES = ROOT / "samples" / "adb_notebooks"


def conv(body: str, language="python"):
    prefix = "# Databricks notebook source\n" if language == "python" else "-- Databricks notebook source\n"
    out, items, ctx, cl, ml, w = convert_text(prefix + body, "t.py", language)
    return out, items, ctx, cl, ml


def src(out, idx=0):
    return "\n".join(out[idx][2])


def levels(items):
    return {i.rule_id: i.level for i in items}


class Parser(unittest.TestCase):
    def test_cells_and_magics(self):
        text = ("# Databricks notebook source\n# MAGIC %md\n# MAGIC hi\n\n# COMMAND ----------\n\nx = 1\n\n"
                "# COMMAND ----------\n\n# MAGIC %sql\n# MAGIC select 1\n")
        cells, _ = parse_export(text, "python")
        self.assertEqual([c.kind for c in cells], ["markdown", "code", "sql"])
        self.assertEqual(cells[2].lines, ["select 1"])
        self.assertEqual(cells[1].start_line, 7)

    def test_missing_header_warns(self):
        cells, warn = parse_export("x = 1\n", "python")
        self.assertTrue(warn)
        self.assertEqual(len(cells), 1)

    def test_empty_cells_dropped_and_dbtitle(self):
        cells, _ = parse_export("# Databricks notebook source\n# COMMAND ----------\n\n# COMMAND ----------\n# DBTITLE 1,T\nx=1\n", "python")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].title, "T")


class Rules(unittest.TestCase):
    def test_fs_auto(self):
        out, items, *_ = conv("files = dbutils.fs.ls('abfss://a@b.dfs.core.windows.net/x')\ndbutils.fs.rm('p', True)\n")
        self.assertIn("notebookutils.fs.ls(", src(out))
        self.assertIn("notebookutils.fs.rm(", src(out))
        self.assertNotIn("dbutils", src(out))
        self.assertEqual({i.level for i in items}, {AUTO})

    def test_widgets_become_parameters(self):
        out, items, ctx, *_ = conv('dbutils.widgets.text("d", "2025-01-01", "Date")\nv = dbutils.widgets.get("d")\nprint(v)\n')
        self.assertEqual(ctx.params, {"d": "'2025-01-01'"})
        s = src(out)
        self.assertNotIn("dbutils", s)
        self.assertIn("v = d", s)
        self.assertEqual(levels(items)["W1"], REVIEW)

    def test_widget_get_without_definition_gets_default(self):
        _, _, ctx, *_ = conv('x = dbutils.widgets.get("only_get")\n')
        self.assertEqual(ctx.params["only_get"], '""')

    def test_widget_non_literal_is_manual(self):
        out, items, *_ = conv("v = dbutils.widgets.get(name_var)\n")
        self.assertEqual(levels(items).get("W4"), MANUAL)
        self.assertIn("TODO[ADB2FABRIC]", src(out))

    def test_secret_positional_and_keyword(self):
        out, items, ctx, *_ = conv('a = dbutils.secrets.get("kv", "k1")\nb = dbutils.secrets.get(scope="kv2", key="k2")\n')
        s = src(out)
        self.assertIn("getSecret('<KEY_VAULT_URL_FOR_kv>', 'k1')", s)
        self.assertIn("getSecret('<KEY_VAULT_URL_FOR_kv2>', 'k2')", s)
        self.assertEqual({i.level for i in items}, {REVIEW})
        self.assertIn("keyvault", ctx.checklist)

    def test_unknown_dbutils_is_manual_once(self):
        _, items, *_ = conv("dbutils.data.summarize(df)\n")
        self.assertEqual([(i.rule_id, i.level) for i in items], [("Z9", MANUAL)])

    def test_legacy_paths_manual(self):
        for line in ['p = "/mnt/x/y"', 'p = "dbfs:/a"', "p = '/dbfs/a'", 'p = "/Volumes/c/s/v"']:
            _, items, *_ = conv(line + "\n")
            self.assertEqual(levels(items).get("P1"), MANUAL, line)

    def test_abfss_is_silent_checklist(self):
        _, items, ctx, *_ = conv('p = "abfss://c@a.dfs.core.windows.net/x"\n')
        self.assertEqual(items, [])
        self.assertIn("storage", ctx.checklist)

    def test_python_import_does_not_trigger_lakehouse(self):
        _, _, ctx, *_ = conv("from pyspark.sql import functions as F\nfrom collections import Counter\n")
        self.assertNotIn("lakehouse", ctx.checklist)

    def test_dollar_in_python_string_not_flagged_but_in_sql_is(self):
        _, items, *_ = conv('s = f"${price}"\n')
        self.assertNotIn("W7", levels(items))
        _, items, *_ = conv("# MAGIC %sql\n# MAGIC SELECT '${p}'\n")
        self.assertEqual(levels(items).get("W7"), MANUAL)

    def test_manual_comment_not_placed_inside_multiline_string(self):
        out, items, *_ = conv('q = """\nCOPY INTO t FROM \'/mnt/x\'\n"""\n')
        s = src(out)
        self.assertEqual(levels(items).get("X2"), MANUAL)
        body = s.split('q = """', 1)[1].split('"""')[0]
        self.assertNotIn("TODO", body)          # SQL text must stay untouched
        self.assertTrue(s.startswith("# TODO[ADB2FABRIC]"))

    def test_dlt_autoloader_copyinto_manual(self):
        _, items, *_ = conv('import dlt\ndf = spark.readStream.format("cloudFiles").load("x")\n')
        self.assertEqual({levels(items)["X3-IMPORT"], levels(items)["X1"]}, {MANUAL})

    def test_no_substitution_inside_comments(self):
        _, items, *_ = conv("# dbutils.fs.ls('x')\nx = 1\n")
        self.assertEqual(items, [])


class Magics(unittest.TestCase):
    def test_sql_cell(self):
        out, *_ = conv("# MAGIC %sql\n# MAGIC SELECT 1\n")
        self.assertEqual(out[0][1], "sql")
        self.assertEqual(out[0][2][0], "%%sql")

    def test_markdown_cell_and_items_unscored(self):
        out, items, *_ = conv("# MAGIC %md\n# MAGIC # Title\n")
        self.assertEqual(out[0][1], "markdown")
        self.assertEqual(out[0][2], ["# Title"])

    def test_run_converted_by_name(self):
        out, items, ctx, *_ = conv("# MAGIC %run ../shared/Common Functions\n")
        self.assertEqual(out[0][2], ["%run Common"])             # shlex: 2nd token is an argument
        self.assertTrue(any(i.rule_id == "MAG-RUNARGS" and i.level == MANUAL for i in items))
        out, *_ = conv("# MAGIC %run ./common/helpers\n")
        self.assertEqual(out[0][2], ["%run helpers"])

    def test_fs_magic(self):
        out, *_ = conv("# MAGIC %fs ls abfss://c@a.dfs.core.windows.net/x\n")
        self.assertEqual(out[0][2], ["display(notebookutils.fs.ls('abfss://c@a.dfs.core.windows.net/x'))"])
        out, *_ = conv("# MAGIC %fs rm -r /tmp/x\n")
        self.assertIn("True", src(out))

    def test_sh_and_unknown(self):
        out, *_ = conv("# MAGIC %sh\n# MAGIC echo hi\n")
        self.assertEqual(out[0][2], ["!echo hi"])
        out, items, *_ = conv("# MAGIC %conda install x\n")
        self.assertTrue(any(i.rule_id == "MAG-UNK" and i.level == MANUAL for i in items))

    def test_sql_notebook_default_language(self):
        out, items, *_ = conv("SELECT 1;\n", language="sql")
        self.assertEqual(out[0][2][0], "%%sql")


class ReportMetrics(unittest.TestCase):
    def notebook(self, name, levels=(), syntax_errors=()):
        return NotebookResult(
            source=name + ".py", target=name + ".ipynb", language="python",
            cells=1, code_lines=20, manual_lines=int(MANUAL in levels),
            items=[Item(name + ".py", 0, i + 1, "test", level, "test", "", "")
                   for i, level in enumerate(levels)],
            checklist={}, syntax_errors=list(syntax_errors), warnings=[],
        )

    def test_nine_notebooks_have_distinct_completion_and_construct_scores(self):
        results = [self.notebook(str(i)) for i in range(5)]
        results += [
            self.notebook("review", [REVIEW] * 4),
            self.notebook("manual1", [AUTO] * 2 + [MANUAL] * 10),
            self.notebook("manual2", [MANUAL]),
            self.notebook("manual3", [MANUAL]),
        ]
        t = totals(results)
        self.assertEqual((t["notebooks"], t["converted"], t["review"], t["manual"]), (9, 5, 1, 3))
        self.assertAlmostEqual(t["notebook_completion"], 5 / 9)
        self.assertAlmostEqual(t["weighted_coverage"], 4 / 18)
        self.assertAlmostEqual(t["strict_coverage"], 2 / 18)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = write_reports(results, [("skipped.dbc", "unsupported")], root, root)
            doc = report.read_text(encoding="utf-8")
            self.assertIn("<b>55.6%</b>", doc)
            self.assertIn("notebooks fully converted", doc)
            self.assertIn("5 of 9 notebooks", doc)
            self.assertIn("<b>22.2%</b>weighted construct coverage", doc)
            self.assertNotIn(">conversion coverage<", doc)
            payload = json.loads((root / "_report" / "report.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(payload["totals"]["notebook_completion"], 5 / 9)
            self.assertEqual(len(payload["skipped"]), 1)

    def test_syntax_errors_prevent_notebook_completion(self):
        result = self.notebook("invalid", syntax_errors=["invalid syntax"])
        t = totals([result])
        self.assertEqual(t["converted"], 0)
        self.assertEqual(t["manual"], 1)
        self.assertEqual(t["notebook_completion"], 0)
        self.assertEqual(t["weighted_coverage"], 1)

    def test_no_notebooks_has_no_completion_score(self):
        self.assertIsNone(totals([])["notebook_completion"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = write_reports([], [], root, root).read_text(encoding="utf-8")
            self.assertIn("<b>n/a</b>", doc)
            self.assertIn("conic-gradient(#107c10 0%", doc)

    def test_all_portable_notebooks_are_complete(self):
        t = totals([self.notebook("a"), self.notebook("b")])
        self.assertEqual(t["notebook_completion"], 1)
        self.assertEqual(t["weighted_coverage"], 1)
        self.assertEqual(t["constructs"], 0)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name) / "out"
        cls.proc = subprocess.run([sys.executable, "-m", "adb2fabric", str(SAMPLES), "-o", str(base), "--no-open"],
                                  cwd=ROOT, capture_output=True, text=True)
        if cls.proc.returncode != 0:
            raise AssertionError(cls.proc.stdout + cls.proc.stderr)
        cls.out = next(base.parent.glob(base.name + "_*"))
        cls.report = json.loads((cls.out / "_report" / "report.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_and_files(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertTrue((self.out / "_report" / "report.html").exists())
        self.assertTrue((self.out / "_report" / "items.csv").exists())
        self.assertTrue((self.out / "etl" / "01_ingest_orders.ipynb").exists())

    def test_ipynb_is_valid_nbformat(self):
        for f in self.out.rglob("*.ipynb"):
            nb = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual((nb["nbformat"], nb["nbformat_minor"]), (4, 5))
            ids = [c["id"] for c in nb["cells"]]
            self.assertEqual(len(ids), len(set(ids)), f.name)
            for c in nb["cells"]:
                self.assertIn(c["cell_type"], ("code", "markdown"))
                if c["cell_type"] == "code":
                    self.assertIn("outputs", c)
                    self.assertIn("execution_count", c)
                self.assertTrue(all(isinstance(s, str) for s in c["source"]))

    def test_statuses(self):
        st = {n["source"]: n["status"] for n in self.report["notebooks"]}
        self.assertEqual(st["etl/02_clean_orders.py"], "Converted")
        self.assertEqual(st["etl/01_ingest_orders.py"], "Converted - review")
        self.assertEqual(st["etl/03_legacy_pipeline.py"], "Needs manual work")

    def test_plain_helper_skipped(self):
        self.assertTrue(any(s["file"].endswith("plain_helper.py") for s in self.report["skipped"]))
        self.assertFalse(any(n["source"].endswith("plain_helper.py") for n in self.report["notebooks"]))

    def test_coverage_arithmetic(self):
        t = self.report["totals"]
        self.assertEqual(t["constructs"], t["auto"] + t["review_items"] + t["manual_items"])
        self.assertAlmostEqual(t["weighted_coverage"], (t["auto"] + 0.5 * t["review_items"]) / t["constructs"], places=9)
        clean = next(n for n in self.report["notebooks"] if n["source"].endswith("02_clean_orders.py"))
        self.assertEqual(clean["weighted_coverage"], 1.0)
        self.assertAlmostEqual(t["notebook_completion"], t["converted"] / t["notebooks"])
        self.assertIn("notebook completion:", self.proc.stdout)
        self.assertIn("construct coverage :", self.proc.stdout)

    def test_deterministic_notebooks(self):
        again = Path(self.tmp.name) / "again"
        proc = subprocess.run([sys.executable, "-m", "adb2fabric", str(SAMPLES), "-o", str(again), "--no-open"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        again = next(again.parent.glob(again.name + "_*"))
        for f in self.out.rglob("*.ipynb"):
            self.assertEqual(f.read_bytes(), (again / f.relative_to(self.out)).read_bytes(), f.name)

    def test_fail_below_gate(self):
        r = subprocess.run([sys.executable, "-m", "adb2fabric", str(SAMPLES), "-o", str(Path(self.tmp.name) / "g"), "--no-open", "--fail-below", "99"],
                           cwd=ROOT, capture_output=True)
        self.assertEqual(r.returncode, 3)

    def test_missing_input(self):
        r = subprocess.run([sys.executable, "-m", "adb2fabric", str(ROOT / "nope")], cwd=ROOT, capture_output=True)
        self.assertEqual(r.returncode, 2)

    def test_idempotent_on_converted_output(self):
        """Converted notebooks are .ipynb, which the tool ignores: re-running on the output finds nothing and does not crash."""
        r = subprocess.run([sys.executable, "-m", "adb2fabric", str(self.out), "--no-open", "-o", str(Path(self.tmp.name) / "x")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
