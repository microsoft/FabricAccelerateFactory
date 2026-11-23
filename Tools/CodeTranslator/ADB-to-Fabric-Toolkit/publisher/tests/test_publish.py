import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT.parent / "converter"
sys.path.insert(0, str(ROOT))

from a2f_publish import ado, common  # noqa: E402
from a2f_publish.fabric import ApiError, AuthError, Client  # noqa: E402


class Mock(BaseHTTPRequestHandler):
    log = []
    existing = {"01_ingest_orders": "id-existing"}
    throttled = {"done": False}
    ops = {}
    folders = []
    locations = {}
    folder_error = False
    move_error = False
    unconfirmed_move = False
    missing_folder_id = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, headers=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        Mock.log.append(("GET", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer test-token":
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/v1/workspaces":
            return self._send(200, {"value": [{"id": "ws-1", "displayName": "Dev WS"}]})
        if self.path.startswith("/v1/workspaces/ws-1/folders?"):
            if Mock.folder_error:
                return self._send(403, {"message": "Folder access denied"})
            query = parse_qs(urlsplit(self.path).query)
            if query.get("continuationToken") == ["p2+/="]:
                return self._send(200, {"value": Mock.folders[1:]})
            return self._send(200, {"value": Mock.folders[:1], "continuationToken": "p2+/=" if len(Mock.folders) > 1 else None})
        if self.path.startswith("/v1/workspaces/ws-1/items/"):
            item_id = self.path.rsplit("/", 1)[-1]
            return self._send(200, {"id": item_id, "folderId": Mock.locations.get(item_id)})
        if self.path.startswith("/v1/workspaces/ws-1/notebooks"):
            items = [{"id": v, "displayName": k} for k, v in Mock.existing.items()]
            if "continuationToken=p2" in self.path:
                return self._send(200, {"value": items[1:]})
            return self._send(200, {"value": items[:1], "continuationToken": "p2" if len(items) > 1 else None})
        if self.path.startswith("/v1/operations/"):
            return self._send(200, {"status": "Succeeded"})
        self._send(404, {})

    def do_POST(self):
        body = self._body()
        Mock.log.append(("POST", self.path, body))
        if self.path == "/v1/workspaces/ws-1/folders":
            if Mock.missing_folder_id:
                return self._send(201, {})
            folder = dict(body, id=f"folder-{len(Mock.folders) + 1}")
            Mock.folders.append(folder)
            return self._send(201, folder)
        if self.path.endswith("/move"):
            if Mock.move_error:
                return self._send(403, {"message": "Move denied"})
            item_id = self.path.split("/")[-2]
            Mock.locations[item_id] = body["targetFolderId"]
            return self._send(200, {"value": [] if Mock.unconfirmed_move else [{"id": item_id, "folderId": body["targetFolderId"]}]})
        if self.path == "/v1/workspaces/ws-1/notebooks":
            if not Mock.throttled["done"]:
                Mock.throttled["done"] = True
                return self._send(429, {"error": "slow down"}, {"Retry-After": "0"})
            name = body["displayName"]
            Mock.existing[name] = "id-" + name
            Mock.locations["id-" + name] = body.get("folderId")
            if name == "02_clean_orders":  # exercise the long-running-operation path
                host = f"http://127.0.0.1:{self.server.server_port}"
                return self._send(202, {}, {"Location": f"{host}/v1/operations/op1", "Retry-After": "0"})
            return self._send(201, {"id": "id-" + name})
        if self.path.endswith("/updateDefinition"):
            return self._send(200, {})
        if "/pullrequests" in self.path:
            return self._send(201, {"pullRequestId": 42})
        self._send(404, {})


def run_cli(*args, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, "-m", "a2f_publish", *args], cwd=ROOT, capture_output=True, text=True, env=e)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        output_prefix = Path(cls.tmp.name) / "converted"
        r = subprocess.run([sys.executable, "-m", "adb2fabric", str(CONVERTER / "samples" / "adb_notebooks"), "-o", str(output_prefix), "--no-open"],
                           cwd=CONVERTER, capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        cls.conv_out = next(output_prefix.parent.glob(output_prefix.name + "_*"))
        cls.srv = HTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Mock.log.clear()
        Mock.throttled["done"] = False
        Mock.existing.clear()
        Mock.existing["01_ingest_orders"] = "id-existing"
        Mock.folders.clear()
        Mock.locations.clear()
        Mock.folder_error = False
        Mock.move_error = False
        Mock.unconfirmed_move = False
        Mock.missing_folder_id = False


class Selection(Base):
    def test_gate_blocks_manual_notebooks(self):
        nbs = common.discover(self.conv_out)
        sel, blocked, invalid = common.select(nbs, include_manual=False)
        self.assertEqual({n.rel for n in blocked}, {"etl/03_legacy_pipeline.ipynb"})
        self.assertEqual(len(sel), 3)
        self.assertEqual(invalid, [])
        sel2, blocked2, _ = common.select(nbs, include_manual=True)
        self.assertEqual((len(sel2), len(blocked2)), (4, 0))

    def test_invalid_notebook_detected(self):
        d = Path(self.tmp.name) / "bad"
        d.mkdir()
        (d / "x.ipynb").write_text("{not json", encoding="utf-8")
        (d / "y.ipynb").write_text(json.dumps({"nbformat": 3, "cells": []}), encoding="utf-8")
        self.assertEqual([n.status for n in common.discover(d)], ["Invalid", "Invalid"])

    def test_name_collisions_use_path(self):
        d = Path(self.tmp.name) / "coll"
        for sub in ("a", "b"):
            (d / sub).mkdir(parents=True, exist_ok=True)
            (d / sub / "load.ipynb").write_text(json.dumps({"nbformat": 4, "cells": []}), encoding="utf-8")
        (d / "solo.ipynb").write_text(json.dumps({"nbformat": 4, "cells": []}), encoding="utf-8")
        nbs = common.discover(d)
        common.assign_names(nbs)
        self.assertEqual(sorted(n.name for n in nbs), ["a__load", "b__load", "solo"])


class FabricApi(Base):
    def _run(self, *extra, token="test-token"):
        return run_cli("fabric", str(self.conv_out), "--workspace-id", "ws-1", "--api-base", self.api, *extra,
                       env={"FABRIC_TOKEN": token})

    def test_create_skip_existing_and_payload_shape(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        creates = [x for x in Mock.log if x[0] == "POST" and x[1] == "/v1/workspaces/ws-1/notebooks"]
        names = [c[2]["displayName"] for c in creates]
        self.assertIn("02_clean_orders", names)
        self.assertIn("sql_sample", names)
        self.assertNotIn("01_ingest_orders", names)               # exists -> skipped by default
        self.assertNotIn("03_legacy_pipeline", names)              # blocked by the manual-work gate
        d = creates[-1][2]["definition"]
        self.assertEqual(d["format"], "ipynb")
        self.assertEqual(d["parts"][0]["path"], "artifact.content.ipynb")
        self.assertEqual(d["parts"][0]["payloadType"], "InlineBase64")
        nb = json.loads(base64.b64decode(d["parts"][0]["payload"]))
        self.assertEqual(nb["nbformat"], 4)
        self.assertNotIn("folderId", creates[-1][2])
        self.assertFalse(any("/folders" in x[1] for x in Mock.log))

    def test_throttle_retried_and_lro_polled(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(any(x[1].startswith("/v1/operations/") for x in Mock.log if x[0] == "GET"))
        self.assertGreater(len([x for x in Mock.log if x[0] == "POST" and x[2].get("displayName") == "02_clean_orders"]), 0)

    def test_update_overwrites_only_with_flag(self):
        self._run("--on-exists", "update")
        ups = [x for x in Mock.log if x[0] == "POST" and x[1].endswith("/updateDefinition")]
        self.assertEqual([u[1] for u in ups], ["/v1/workspaces/ws-1/notebooks/id-existing/updateDefinition"])

    def test_on_exists_fail(self):
        r = self._run("--on-exists", "fail")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stdout)

    def test_dry_run_makes_no_changes(self):
        r = self._run("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([x for x in Mock.log if x[0] == "POST"], [])
        self.assertIn("DRY RUN", r.stdout)

    def test_workspace_by_name(self):
        r = run_cli("fabric", str(self.conv_out), "--workspace-name", "Dev WS", "--api-base", self.api, "--dry-run", env={"FABRIC_TOKEN": "test-token"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_bad_token_gives_exit_2_and_no_token_in_output(self):
        r = self._run(token="wrong-secret-token")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("wrong-secret-token", r.stdout + r.stderr)

    def test_include_needs_manual_publishes_it(self):
        self._run("--include-needs-manual")
        names = [x[2]["displayName"] for x in Mock.log if x[0] == "POST" and x[1] == "/v1/workspaces/ws-1/notebooks"]
        self.assertIn("03_legacy_pipeline", names)

    def test_publish_report_written(self):
        self._run("--dry-run")
        rep = json.loads((self.conv_out / "_report" / "publish_fabric.json").read_text(encoding="utf-8"))
        self.assertTrue(rep["dry_run"])
        self.assertIn("etl/03_legacy_pipeline.ipynb", rep["blocked"])

    def test_missing_input(self):
        self.assertEqual(run_cli("fabric", str(ROOT / "nope"), "--workspace-id", "x").returncode, 2)

    def report(self):
        return json.loads((self.conv_out / "_report" / "publish_fabric.json").read_text(encoding="utf-8"))

    def test_create_nested_destination_and_keep_gate(self):
        r = self._run("--folder", "Migration\\Notebooks")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        folders = [x[2] for x in Mock.log if x[0] == "POST" and x[1].endswith("/folders")]
        self.assertEqual(folders, [{"displayName": "Migration"}, {"displayName": "Notebooks", "parentFolderId": "folder-1"}])
        creates = [x[2] for x in Mock.log if x[0] == "POST" and x[1].endswith("/notebooks")]
        self.assertTrue(creates)
        self.assertTrue(all(x["folderId"] == "folder-2" for x in creates))
        self.assertNotIn("03_legacy_pipeline", [x["displayName"] for x in creates])
        self.assertFalse(any(x[1].endswith("/move") for x in Mock.log))
        self.assertIn("etl/03_legacy_pipeline.ipynb", self.report()["blocked"])
        self.assertEqual(self.report()["destination_folder"]["id"], "folder-2")

    def test_reuse_correct_parent_and_paginated_folders(self):
        Mock.folders.extend([
            {"id": "other", "displayName": "Notebooks"},
            {"id": "parent", "displayName": "Migration"},
            {"id": "dest", "displayName": "Notebooks", "parentFolderId": "parent"},
        ])
        r = self._run("--folder", "migration/NOTEBOOKS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.report()["destination_folder"]["id"], "dest")
        self.assertFalse(any(x[0] == "POST" and x[1].endswith("/folders") for x in Mock.log))
        self.assertTrue(any("continuationToken=p2%2B%2F%3D" in x[1] for x in Mock.log))

    def test_partial_path_creates_only_missing_child(self):
        Mock.folders.append({"id": "parent", "displayName": "Migration"})
        r = self._run("--folder", "Migration\\Notebooks")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        posts = [x[2] for x in Mock.log if x[0] == "POST" and x[1].endswith("/folders")]
        self.assertEqual(posts, [{"displayName": "Notebooks", "parentFolderId": "parent"}])

    def test_folder_dry_run_never_writes_or_assigns_fake_ids(self):
        r = self._run("--folder", "Migration\\Notebooks", "--on-exists", "update", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(x[0] == "POST" for x in Mock.log))
        dest = self.report()["destination_folder"]
        self.assertIsNone(dest["id"])
        self.assertEqual([s["action"] for s in dest["steps"]], ["would create", "would create"])
        self.assertTrue(all(s["id"] is None for s in dest["steps"]))
        self.assertIn("folder would create: Migration\\Notebooks", r.stdout)

    def test_update_moves_but_skip_does_not(self):
        r = self._run("--folder", "Migration", "--on-exists", "update")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        moves = [x for x in Mock.log if x[0] == "POST" and x[1].endswith("/move")]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][1:], ("/v1/workspaces/ws-1/items/id-existing/move", {"targetFolderId": "folder-1"}))
        self.assertEqual(Mock.locations["id-existing"], "folder-1")
        result = next(x for x in self.report()["results"] if x["action"] == "update")
        self.assertTrue(result["content_updated"])
        self.assertEqual(result["folder_placement"], "in destination")

    def test_update_in_same_folder_does_not_move(self):
        Mock.folders.append({"id": "dest", "displayName": "Migration"})
        Mock.locations["id-existing"] = "dest"
        r = self._run("--folder", "Migration", "--on-exists", "update")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(x[1].endswith("/move") for x in Mock.log))

    def test_no_folder_update_preserves_existing_location(self):
        Mock.locations["id-existing"] = "old-folder"
        r = self._run("--on-exists", "update")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Mock.locations["id-existing"], "old-folder")
        self.assertFalse(any(x[1].endswith("/move") for x in Mock.log))

    def test_folder_errors_stop_instead_of_publishing_to_root(self):
        Mock.folder_error = True
        r = self._run("--folder", "Migration")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(any(x[0] == "POST" for x in Mock.log))
        self.assertIn("Folder access denied", self.report()["error"])

    def test_create_folder_without_id_stops_notebook_uploads(self):
        Mock.missing_folder_id = True
        r = self._run("--folder", "Migration")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("returned no folder ID", self.report()["error"])
        self.assertFalse(any(x[0] == "POST" and x[1].endswith("/notebooks") for x in Mock.log))

    def test_ambiguous_folder_is_rejected(self):
        Mock.folders.extend([
            {"id": "a", "displayName": "Migration"},
            {"id": "b", "displayName": "migration"},
        ])
        r = self._run("--folder", "Migration")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Ambiguous", self.report()["error"])
        self.assertFalse(any(x[0] == "POST" for x in Mock.log))

    def test_move_failure_reports_partial_update(self):
        Mock.move_error = True
        r = self._run("--folder", "Migration", "--on-exists", "update")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        result = next(x for x in self.report()["results"] if x["action"] == "update")
        self.assertEqual(result["result"], "failed")
        self.assertTrue(result["content_updated"])
        self.assertIn("content was updated", result["error"])
        self.assertIn("Move denied", result["error"])

    def test_unconfirmed_move_is_not_success(self):
        Mock.unconfirmed_move = True
        r = self._run("--folder", "Migration", "--on-exists", "update")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not confirm", r.stdout)

    def test_invalid_folder_path_is_rejected_before_network(self):
        for path in ("Migration\\\\Notebooks", "..\\bad", "\\absolute", " spaced", "trailing ",
                     "bad:name", "bad&name", "recycled", "x" * 256, "a\\b\\c\\d\\e\\f\\g\\h\\i\\j\\k"):
            with self.subTest(path=path):
                Mock.log.clear()
                r = self._run("--folder", path)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(Mock.log, [])

    def test_folder_with_manual_override(self):
        r = self._run("--folder", "Migration", "--include-needs-manual")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        creates = [x[2] for x in Mock.log if x[0] == "POST" and x[1].endswith("/notebooks")]
        legacy = next(x for x in creates if x["displayName"] == "03_legacy_pipeline")
        self.assertEqual(legacy["folderId"], "folder-1")
        self.assertEqual(self.report()["blocked"], [])

    def test_all_existing_skip_does_not_create_folders(self):
        Mock.existing.update({"02_clean_orders": "id-clean", "sql_sample": "id-sql"})
        r = self._run("--folder", "Unused")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(x[0] == "POST" for x in Mock.log))
        self.assertFalse(self.report()["destination_folder"]["used"])
        self.assertTrue(all(x["folder_placement"] == "unchanged" for x in self.report()["results"]))

    @unittest.skipUnless(sys.platform == "win32", "Windows batch launcher")
    def test_launcher_folder_and_blocked_consent(self):
        for consent in ("n", "y"):
            with self.subTest(consent=consent):
                self.setUp()
                env = dict(os.environ, FABRIC_TOKEN="test-token", A2F_CHILD="1")
                # Route only this launcher's client to the local mock; never use live credentials/services.
                with tempfile.TemporaryDirectory() as tmp:
                    launch_root = Path(tmp)
                    package = launch_root / "a2f_publish"
                    shutil.copytree(ROOT / "a2f_publish", package)
                    shutil.copy2(ROOT / "Publish-Notebooks.bat", launch_root)
                    fabric_file = package / "fabric.py"
                    fabric_file.write_text(
                        fabric_file.read_text(encoding="utf-8").replace(
                            'API_BASE = "https://api.fabric.microsoft.com/v1"', f'API_BASE = "{self.api}"'
                        ), encoding="utf-8"
                    )
                    answers = launch_root / "answers.txt"
                    answers.write_bytes(f"1\r\nws-1\r\nU\r\nMigration Team\\Notebooks\r\n{consent}\r\ny\r\n\r\n".encode())
                    with answers.open("r") as stdin:
                        proc = subprocess.run(
                            f'"{os.environ.get("ComSpec", "cmd.exe")}" /d /c call '
                            f'"{launch_root / "Publish-Notebooks.bat"}" "{self.conv_out}"',
                            stdin=stdin, capture_output=True, text=True, env=env, timeout=60,
                        )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("DRY RUN", proc.stdout)
                self.assertIn("Deploy these blocked notebooks", proc.stdout)
                self.assertIn("Done.", proc.stdout)
                rep = self.report()
                self.assertFalse(rep["dry_run"])
                self.assertEqual(rep["destination_folder"]["path"], "Migration Team\\Notebooks")
                self.assertEqual(rep["destination_folder"]["id"], "folder-2")
                legacy = [x for x in rep["results"] if x["name"] == "03_legacy_pipeline"]
                self.assertEqual(bool(legacy), consent == "y")
                self.assertEqual(bool(rep["blocked"]), consent == "n")


class AdoGit(Base):
    def setUp(self):
        super().setUp()
        self.bare = Path(self.tmp.name) / f"remote_{self._testMethodName}.git"
        seed = Path(self.tmp.name) / f"seed_{self._testMethodName}"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README.md").write_text("repo", encoding="utf-8")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.y", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.y")
        for cmd in (["add", "-A"], ["commit", "-q", "-m", "seed"], ["remote", "add", "origin", str(self.bare)], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", *cmd], cwd=seed, check=True, env=env)

    def _git(self, *args):
        r = subprocess.run(["git", f"--git-dir={self.bare}", *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)   # fail loudly: an empty result must mean "empty", not "git error"
        return r.stdout

    def branches(self):
        return self._git("for-each-ref", "--format=%(refname:short)")

    def files(self, branch):
        return self._git("ls-tree", "-r", "--name-only", branch).split()

    def test_pushes_new_branch_not_main(self):
        r = run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--branch", "feature/convert")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("feature/convert", self.branches())
        fl = self.files("feature/convert")
        self.assertIn("notebooks/etl/02_clean_orders.ipynb", fl)
        self.assertIn("notebooks/publish_manifest.json", fl)
        self.assertNotIn("notebooks/etl/03_legacy_pipeline.ipynb", fl)    # gated
        self.assertEqual(self.files("main"), ["README.md"])               # main untouched

    def test_rerun_against_published_branch_has_no_changes(self):
        run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--branch", "b1")
        r = run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--base-branch", "b1", "--branch", "b2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no changes", r.stdout)
        self.assertNotIn("b2", self.branches())

    def test_dry_run_pushes_nothing(self):
        r = run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--branch", "dry", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("dry", self.branches())

    def test_unknown_base_branch_fails_cleanly(self):
        r = run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--base-branch", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR", r.stderr)

    def test_pat_never_in_output(self):
        r = run_cli("ado", str(self.conv_out), "--repo-url", str(self.bare), "--branch", "pat", env={"ADO_PAT": "SUPERSECRETPAT"})
        self.assertNotIn("SUPERSECRETPAT", r.stdout + r.stderr)

    def test_parse_ado_url(self):
        self.assertEqual(ado.parse_ado_url("https://dev.azure.com/org/My%20Proj/_git/repo"), ("org", "My Proj", "repo"))
        self.assertEqual(ado.parse_ado_url("https://org.visualstudio.com/proj/_git/repo"), ("org", "proj", "repo"))
        self.assertIsNone(ado.parse_ado_url("C:/x/y.git"))

    def test_pull_request_payload(self):
        os.environ["ADO_PAT"] = "pat"
        try:
            url = ado.create_pull_request("https://dev.azure.com/org/proj/_git/repo", "feat", "main", "T", "D", api_base=f"http://127.0.0.1:{self.srv.server_port}")
        finally:
            os.environ.pop("ADO_PAT")
        self.assertTrue(url.endswith("/pullrequest/42"))
        post = [x for x in Mock.log if x[0] == "POST" and "/pullrequests" in x[1]][0]
        self.assertEqual(post[2]["sourceRefName"], "refs/heads/feat")
        self.assertEqual(post[2]["targetRefName"], "refs/heads/main")


class Config(unittest.TestCase):
    def test_config_expansion_cli_wins(self):
        from a2f_publish.cli import expand_config
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps({"target": "fabric", "input": "X", "workspace_id": "w", "on_exists": "update", "folder": "Migration\\Notebooks", "dry_run": True, "_note": "ignored"}), encoding="utf-8")
        out = expand_config(["--config", str(p), "--on-exists", "skip", "--folder", "Other"])
        self.assertEqual(out[:2], ["fabric", "X"])
        self.assertIn("--dry-run", out)
        self.assertEqual(out.count("--on-exists"), 1)
        self.assertEqual(out[out.index("--on-exists") + 1], "skip")
        self.assertEqual(out.count("--folder"), 1)
        self.assertEqual(out[out.index("--folder") + 1], "Other")


if __name__ == "__main__":
    unittest.main(verbosity=1)
