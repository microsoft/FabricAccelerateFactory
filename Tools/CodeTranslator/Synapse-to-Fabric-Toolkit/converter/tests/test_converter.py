import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from syn2fabric.config import DEFAULT_CONFIG, load_settings  # noqa: E402
from syn2fabric.converter import build_notebook, convert_cells  # noqa: E402
from syn2fabric.model import AUTO, MANUAL, REVIEW  # noqa: E402
from syn2fabric.reader import NotebookSkipped, load_notebook  # noqa: E402

DEFAULT = load_settings()


def notebook(*cells, language="python", kernel="synapse_pyspark", meta=None):
    entries = []
    for cell in cells:
        kind, text, tags = ("code", cell, []) if isinstance(cell, str) else (cell + ([],))[:3]
        entries.append({"cell_type": kind, "metadata": {"tags": tags} if tags else {}, "source": text.splitlines(True),
                        "outputs": [{"output_type": "stream", "text": ["secret output"]}] if kind == "code" else [],
                        "execution_count": 3 if kind == "code" else None})
    metadata = {"kernelspec": {"name": kernel}, "language_info": {"name": language}}
    metadata.update(meta or {})
    return {"nbformat": 4, "nbformat_minor": 2, "metadata": metadata, "cells": entries}


def settings_with(tmp, **overrides):
    data = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    data.update(copy.deepcopy(overrides))
    path = Path(tmp) / "rules.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_settings(path)


class Result:
    def __init__(self, nb_dict, cfg=DEFAULT):
        nb = load_notebook(json.dumps(nb_dict))
        self.cells, self.items, self.ctx, self.code_lines, self.manual_lines, self.warnings = convert_cells(nb, "t.ipynb", cfg)
        self.cfg = cfg

    def code(self, n=0):
        return "\n".join([c for c in self.cells if c[1] != "markdown"][n][2])

    def item(self, rule_id):
        return next(i for i in self.items if i.rule_id == rule_id)

    def ids(self):
        return [i.rule_id for i in self.items]


class Utilities(unittest.TestCase):
    def test_mssparkutils_is_renamed(self):
        r = Result(notebook("from notebookutils import mssparkutils\nmssparkutils.fs.ls('Files')\nmssparkutils.notebook.exit('x')"))
        code = r.code()
        self.assertIn("import notebookutils", code)
        self.assertIn("notebookutils.fs.ls('Files')", code)
        self.assertIn("notebookutils.notebook.exit('x')", code)
        self.assertNotIn("mssparkutils", code)
        self.assertEqual({i.level for i in r.items}, {AUTO})

    def test_get_secret_derives_vault_url(self):
        r = Result(notebook('pw = mssparkutils.credentials.getSecret("kv-prod", "db-password")'))
        self.assertIn('notebookutils.credentials.getSecret("https://kv-prod.vault.azure.net/", "db-password")', r.code())
        self.assertEqual(r.item("S1").level, AUTO)
        self.assertIn("keyvault", r.ctx.checklist)

    def test_get_secret_uses_configured_vault_and_drops_linked_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings_with(tmp, key_vaults={"kv-prod": "https://contoso-prod.vault.azure.net/"})
            r = Result(notebook('pw = TokenLibrary.getSecret("kv-prod", "db", "LS1")'), cfg)
        self.assertIn('getSecret("https://contoso-prod.vault.azure.net/", "db")', r.code())
        self.assertNotIn("LS1", r.code())
        self.assertEqual(r.item("S1").level, AUTO)

    def test_get_secret_with_linked_service_placeholder_then_configured(self):
        source = 'pw = mssparkutils.credentials.getSecretWithLS("kvLS", "db")'
        r = Result(notebook(source))
        self.assertIn('"<KEY_VAULT_URL_FOR_kvLS>"', r.code())
        self.assertEqual(r.item("S2").level, AUTO)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings_with(tmp, linked_services={"kvLS": "https://contoso.vault.azure.net/"})
            configured = Result(notebook(source), cfg)
        self.assertIn('getSecret("https://contoso.vault.azure.net/", "db")', configured.code())
        self.assertEqual(configured.item("S2").level, AUTO)

    def test_dynamic_secret_arguments_are_manual_and_unchanged(self):
        r = Result(notebook("pw = mssparkutils.credentials.getSecret(vault_name, secret_name)"))
        self.assertIn("mssparkutils.credentials.getSecret(vault_name, secret_name)", r.code())
        self.assertIn("TODO[SYN2FABRIC] S3", r.code())
        self.assertEqual(r.item("S3").level, MANUAL)
        self.assertNotIn("U1", r.ids())          # the generic rename must not touch a manual line

    def test_token_library_is_manual(self):
        r = Result(notebook('props = TokenLibrary.getPropertiesAsMap("ls")'))
        self.assertEqual(r.item("S3").level, MANUAL)
        self.assertEqual(r.manual_lines, 1)

    def test_env_context_and_token(self):
        r = Result(notebook('print(mssparkutils.env.getPoolName())\nt = mssparkutils.credentials.getToken("Storage")\nmssparkutils.env.getFoo()'))
        self.assertIn('notebookutils.runtime.context.get("poolName")', r.code())
        self.assertIn('notebookutils.credentials.getToken("Storage")', r.code())
        self.assertEqual(r.item("E1").level, REVIEW)
        self.assertEqual(r.item("T1").level, REVIEW)
        self.assertEqual(r.item("E2").level, MANUAL)

    def test_secret_conversion_also_applies_to_scala_cells(self):
        r = Result(notebook('val s = mssparkutils.credentials.getSecret("kv", "s")', language="scala", kernel="synapse_spark"))
        self.assertIn('notebookutils.credentials.getSecret("https://kv.vault.azure.net/", "s")', r.code())
        self.assertEqual(r.item("S1").level, AUTO)
        self.assertNotIn("[SYN2FABRIC]", r.code())


class Catalog(unittest.TestCase):
    def test_catalog_apis(self):
        r = Result(notebook("a = spark.catalog.listDatabases()\nb = spark.catalog.currentDatabase()\n"
                            "c = spark.catalog.getDatabase(db)\nd = spark.catalog.listFunctions()\n"
                            "spark.catalog.registerFunction('f', fn)"))
        code = r.code()
        self.assertIn('spark.sql("SHOW DATABASES")', code)
        self.assertIn('spark.sql("SELECT CURRENT_DATABASE()").first()[0]', code)
        self.assertIn('spark.sql("DESCRIBE DATABASE " + str(db))', code)
        self.assertIn("spark.udf.register('f', fn)", code)
        self.assertEqual(r.item("CAT5").level, MANUAL)
        self.assertEqual(r.item("CAT2").level, AUTO)

    def test_linked_service_and_sql_connector_are_manual(self):
        r = Result(notebook('spark.conf.set("spark.storage.synapse.linkedServiceName", "ls")\n'
                            'df = spark.read.synapsesql("pool.dbo.t")\npath = "/user/trusted-service-user/x"'))
        for rule in ("L1", "Q1", "P1"):
            self.assertEqual(r.item(rule).level, MANUAL, rule)
        self.assertEqual({"linkedservice", "sqlconnector", "storage"} - set(r.ctx.checklist), set())


class Cells(unittest.TestCase):
    def test_cell_magics(self):
        r = Result(notebook("%%pyspark\nprint(1)", "%%sql\nSELECT 1", "%%spark\nval x = 1", "%%csharp\nvar y = 1;",
                            "%%configure -f\n{\"driverMemory\": \"8g\"}", "%%weird\nfoo"))
        kinds = [c[1] for c in r.cells]
        self.assertEqual(kinds, ["code", "sql", "scala", "code", "code", "code"])
        self.assertEqual(r.cells[0][2], ["print(1)"])
        self.assertEqual(r.item("MAG-CS").level, MANUAL)
        self.assertEqual(r.item("MAG-CFG").level, REVIEW)
        self.assertEqual(r.item("MAG-UNK").level, MANUAL)
        self.assertTrue(r.cells[3][2][0].startswith("# TODO[SYN2FABRIC] MAG-CS"))
        self.assertEqual(r.cells[4][2][0], "%%configure -f")
        self.assertIn("sessionconf", r.ctx.checklist)
        self.assertIn("dotnet", r.ctx.checklist)

    def test_default_language_cells_get_explicit_magic_prefix(self):
        for language, kernel, prefix in (("sql", "synapse_sql", "%%sql"), ("scala", "synapse_spark", "%%spark"), ("r", "synapse_sparkr", "%%sparkr")):
            r = Result(notebook("SELECT 1", language=language, kernel=kernel))
            out = build_notebook("t.ipynb", r.cells, r.ctx, r.items, DEFAULT)
            self.assertEqual(out["cells"][1]["source"][0].strip(), prefix)

    def test_default_csharp_notebook_is_manual(self):
        r = Result(notebook("var x = 1;", language="csharp", kernel="synapse_sparkdotnet"))
        self.assertEqual(r.item("MAG-CS").level, MANUAL)

    def test_run_magic_uses_notebook_name_and_mapping(self):
        r = Result(notebook('%run /shared/utils/Common_Utils { "env": "dev" }\n%run "Folder/Other"'))
        self.assertIn('%run Common_Utils { "env": "dev" }', r.code())
        self.assertIn("%run Other", r.code())
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings_with(tmp, notebook_mappings={"Common_Utils": "Common Utilities"})
            mapped = Result(notebook("%run /shared/Common_Utils"), cfg)
        self.assertIn('%run "Common Utilities"', mapped.code())
        self.assertEqual(mapped.item("R1").level, AUTO)
        with tempfile.TemporaryDirectory() as tmp:
            by_path = Result(notebook("%run /CustomNotebooks/Shared/sharedFunctions"),
                             settings_with(tmp, notebook_mappings={"/CustomNotebooks/Shared/sharedFunctions": "sharedFunctions_Fabric"}))
        self.assertIn("%run sharedFunctions_Fabric", by_path.code())
        self.assertEqual(by_path.item("R1").level, AUTO)
        self.assertEqual(r.item("R1").level, AUTO)
        self.assertIn("run", r.ctx.checklist)

    def test_pip_is_kept_and_adds_checklist_without_a_finding(self):
        r = Result(notebook("%pip install pandas\nimport pandas"))
        self.assertIn("%pip install pandas", r.code())
        self.assertIn("library", r.ctx.checklist)
        self.assertEqual(r.items, [])

    def test_comments_and_strings_do_not_create_findings_on_comment_lines(self):
        r = Result(notebook("# mssparkutils.fs.ls('x')\nprint('ok')"))
        self.assertEqual(r.items, [])

    def test_inline_tags_do_not_break_magic_or_multiline_strings(self):
        r = Result(notebook('x = """\nmssparkutils.fs.ls(1)\n"""\n'))
        self.assertTrue(any(line.startswith("# ") for line in r.cells[0][2]) or "[SYN2FABRIC]" not in r.code())
        compile(r.code(), "cell", "exec")


class Mappings(unittest.TestCase):
    def test_path_and_table_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings_with(
                tmp,
                path_mappings=[{"id": "raw", "source": "abfss://raw@acct.dfs.core.windows.net/",
                                "target": "abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Files/raw/"}],
                table_mappings={"salesdb.orders": "lakehouse.sales.orders"})
            r = Result(notebook('df = spark.read.parquet("abfss://raw@acct.dfs.core.windows.net/sales")\n'
                                'df.write.saveAsTable("salesdb.orders")\nspark.table("other.salesdb.orders")'), cfg)
        code = r.code()
        self.assertIn('"abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Files/raw/sales"', code)
        self.assertIn('saveAsTable("lakehouse.sales.orders")', code)
        self.assertIn('other.salesdb.orders', code)            # not a whole identifier match
        self.assertEqual({"P-MAP", "P-TBL"}, set(r.ids()) & {"P-MAP", "P-TBL"})
        self.assertNotIn("P2", r.ids())                          # mapped OneLake path is not flagged again
        self.assertEqual(r.item("P-MAP").level, AUTO)

    def test_unmapped_external_storage_is_review(self):
        r = Result(notebook('spark.read.parquet("wasbs://c@a.blob.core.windows.net/x")'))
        self.assertEqual(r.item("P2").level, REVIEW)


class Notebook(unittest.TestCase):
    def test_output_is_valid_fabric_notebook_without_outputs(self):
        r = Result(notebook(("markdown", "# Title"), ("code", "print(1)", ["parameters"]), "mssparkutils.fs.ls('x')"))
        out = build_notebook("t.ipynb", r.cells, r.ctx, r.items, DEFAULT)
        self.assertEqual(out["nbformat"], 4)
        ids = [c["id"] for c in out["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        for cell in out["cells"]:
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
                self.assertIn("microsoft", cell["metadata"])
        self.assertEqual(out["cells"][2]["metadata"]["tags"], ["parameters"])
        self.assertEqual(out["metadata"]["kernel_info"]["name"], "synapse_pyspark")
        self.assertNotIn("a365ComputeOptions", out["metadata"])
        self.assertEqual(out["metadata"]["syn2fabric"]["source"], "t.ipynb")

    def test_pool_and_session_settings_reach_the_checklist(self):
        meta = {"a365ComputeOptions": {"name": "pool1", "sparkVersion": "3.4", "nodeCount": 5, "cores": 8, "memory": 56,
                                       "id": "/subscriptions/00000000-0000-0000-0000-000000000000/x"}}
        r = Result(notebook("print(1)", meta=meta))
        detail = " ".join(r.ctx.checklist["pool"])
        self.assertIn("pool1", detail)
        self.assertIn("Spark 3.4", detail)
        self.assertNotIn("00000000", detail)

    def test_workspace_json_artifact(self):
        artifact = {"name": "nb", "properties": {"bigDataPool": {"referenceName": "p"}, "sessionProperties": {"driverMemory": "28g", "conf": {"a": "b"}},
                                                  "metadata": {"language_info": {"name": "python"}},
                                                  "cells": [{"cell_type": "code", "metadata": {}, "source": ["print(1)"]}]}}
        nb = load_notebook(json.dumps(artifact))
        self.assertEqual(nb.source_format, "workspace-json")
        r = Result(artifact)
        self.assertIn("sessionconf", r.ctx.checklist)
        self.assertIn("driverMemory=28g", " ".join(r.ctx.checklist["sessionconf"]))


class Reader(unittest.TestCase):
    def test_skip_reasons(self):
        cases = {
            "{broken": "not valid JSON",
            "[]": "not a notebook",
            json.dumps({"name": "p", "properties": {"activities": []}}): "not a Synapse notebook",
            json.dumps(notebook("x", kernel="python3")): "no Synapse notebook metadata",
            json.dumps(notebook("x", meta={"microsoft": {"language": "python"}})): "already a Fabric notebook",
        }
        for text, reason in cases.items():
            with self.assertRaises(NotebookSkipped, msg=text) as ctx:
                load_notebook(text)
            self.assertIn(reason, str(ctx.exception))

    def test_include_plain_accepts_generic_notebooks(self):
        nb = load_notebook(json.dumps(notebook("print(1)", kernel="python3")), include_plain=True)
        self.assertEqual(nb.language, "python")

    def test_empty_cells_are_dropped_and_unsupported_types_warned(self):
        data = notebook("   \n", "print(1)")
        data["cells"].append({"cell_type": "raw", "metadata": {}, "source": ["x"]})
        nb = load_notebook(json.dumps(data))
        r = Result(data)
        self.assertEqual(len(r.cells), 1)
        self.assertEqual(len(nb.warnings), 1)


if __name__ == "__main__":
    unittest.main()
