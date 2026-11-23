from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .ado import GitError, create_pull_request, push_to_repo
from .common import NEEDS_MANUAL, assign_names, discover, select
from .fabric import API_BASE, ApiError, AuthError, Client, publish

OK, FAILED, USAGE, NOTHING = 0, 1, 2, 3


def build_parser():
    ap = argparse.ArgumentParser(prog="a2f_publish", description="Publish converted Fabric notebooks (.ipynb) to a Fabric workspace or an Azure DevOps repo.")
    ap.add_argument("--config", help="JSON file with the options below (see publish.config.sample.json)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="target", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("input", help="output folder of adb2fabric (contains the .ipynb files and _report)")
    common.add_argument("--include-needs-manual", action="store_true", help="also publish notebooks that still contain TODO[ADB2FABRIC] markers (blocked by default)")
    common.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    f = sub.add_parser("fabric", parents=[common], help="create/update notebook items in a Fabric workspace")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--workspace-id")
    g.add_argument("--workspace-name")
    f.add_argument("--on-exists", choices=["skip", "update", "fail"], default="skip", help="when a notebook with the same name exists (default: skip; update OVERWRITES its content)")
    f.add_argument("--description", default="Converted from Azure Databricks by adb2fabric")
    f.add_argument("--folder", default="", help="destination workspace folder path (create/reuse, including nested folders); omit for existing root behaviour. Update also moves existing notebooks; skip never moves them.")
    f.add_argument("--api-base", default=API_BASE)
    a = sub.add_parser("ado", parents=[common], help="push notebooks to a new branch of an Azure DevOps (git) repo")
    a.add_argument("--repo-url", required=True, help="https://dev.azure.com/<org>/<project>/_git/<repo>")
    a.add_argument("--base-branch", default="main")
    a.add_argument("--branch", help="new branch to create (default adb2fabric/<timestamp>)")
    a.add_argument("--folder", default="notebooks", help="folder inside the repo (default: notebooks)")
    a.add_argument("--message", default="Add notebooks converted from Azure Databricks")
    a.add_argument("--create-pr", action="store_true", help="open a pull request into the base branch")
    a.add_argument("--ado-api-base", help=argparse.SUPPRESS)
    return ap


def expand_config(argv):
    """--config file.json  ->  equivalent command-line arguments (explicit CLI arguments win)."""
    if "--config" not in argv:
        return argv
    i = argv.index("--config")
    cfg = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8"))
    rest = argv[:i] + argv[i + 2:]
    out = [str(cfg.pop("target")), str(cfg.pop("input"))]
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        flag = "--" + k.replace("_", "-")
        if flag in rest:
            continue
        if v is True:
            out.append(flag)
        elif v not in (False, None, ""):
            out += [flag, str(v)]
    return out + rest


class _Tee:
    """Mirror console output into a log file so every run leaves a record."""
    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, s):
        self.stream.write(s)
        self.log.write(s)
        self.log.flush()

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def main(argv=None) -> int:
    argv = expand_config(list(sys.argv[1:] if argv is None else argv))
    if argv[:1] == ["--blocked-count"]:      # helper for the launcher: list blocked notebooks (stderr) and print their count (stdout)
        folder = Path(argv[1]).expanduser().resolve() if len(argv) > 1 else Path(".")
        _, blocked, _ = select(discover(folder), include_manual=False) if folder.is_dir() else ([], [], [])
        for n in blocked:
            print(f"  BLOCKED  {n.rel}  ({n.todo} TODO marker(s))", file=sys.stderr)
        print(len(blocked))
        return OK
    args = build_parser().parse_args(argv)
    root = Path(args.input).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: input folder not found: {root}", file=sys.stderr)
        return USAGE
    (root / "_report").mkdir(exist_ok=True)
    log_path = root / "_report" / "publish.log"
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')}  a2f_publish {' '.join(a for a in argv[:1])} ... =====\n")
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
    try:
        code = _run(args, root)
        print(f"  log: {log_path}")
        return code
    except Exception as e:  # never die silently
        print(f"\nUNEXPECTED ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"  log: {log_path}")
        return FAILED
    finally:
        sys.stdout, sys.stderr = old
        log.close()


def _run(args, root) -> int:
    nbs = discover(root)
    selected, blocked, invalid = select(nbs, args.include_needs_manual)
    assign_names(selected)
    print(f"a2f_publish {__version__}  target={args.target}{'  (DRY RUN)' if args.dry_run else ''}")
    print(f"  found {len(nbs)} notebook(s): {len(selected)} to publish, {len(blocked)} blocked (still need manual work), {len(invalid)} invalid")
    for n in blocked:
        print(f"  BLOCKED  {n.rel}  ({n.todo} TODO marker(s)); fix them or use --include-needs-manual")
    for n in invalid:
        print(f"  INVALID  {n.rel}: {n.error}")
    if not selected:
        print("Nothing to publish.")
        return NOTHING
    warn = [n for n in selected if n.placeholders]
    for n in warn:
        print(f"  WARNING  {n.rel}: {n.placeholders} unresolved <KEY_VAULT_URL_FOR_...> placeholder(s); the notebook will fail until you set the vault URL")

    report = {"tool": "a2f_publish", "version": __version__, "time": datetime.now().isoformat(timespec="seconds"), "target": args.target,
              "dry_run": args.dry_run, "blocked": [n.rel for n in blocked], "invalid": [n.rel for n in invalid]}
    code = OK
    try:
        if args.target == "fabric":
            report["destination_folder"] = {"path": args.folder}
            print(f"  destination folder: {args.folder or '(workspace root; existing notebook locations retained)'}")
            if args.folder:
                print("  Folder APIs are Preview. Update overwrites content and moves existing notebooks; Skip leaves them unchanged.")
            client = Client(args.api_base)
            ws = args.workspace_id or client.workspace_id_by_name(args.workspace_name)
            report["workspace_id"] = ws
            deployment = publish(client, ws, selected, args.on_exists, args.dry_run, args.description, args.folder)
            report.update(deployment)
            results = deployment["results"]
            for step in deployment["destination_folder"]["steps"]:
                print(f"  folder {step['action']}: {step['path']}")
            if args.folder and not deployment["destination_folder"]["used"]:
                print("  No notebooks to create/update; no folders were created and no items moved.")
            print(f"\n  {'notebook':<44}{'action':<9}result")
            for r in results:
                print(f"  {r['name'][:42]:<44}{r['action']:<9}{r['result']}{'  ' + r['error'] if r['error'] else ''}")
            if any(r["result"] == "failed" for r in results):
                code = FAILED
        else:
            res = push_to_repo(selected, args.repo_url, args.base_branch, args.branch, args.folder, args.message, args.dry_run)
            report["git"] = res
            states = {s: sum(1 for f in res["files"] if f["state"] == s) for s in ("new", "modified", "unchanged")}
            print(f"\n  branch {res['branch']} (from {res['base']}), folder '{res['folder']}': {states['new']} new, {states['modified']} modified, {states['unchanged']} unchanged")
            if args.dry_run:
                print("  dry run: nothing committed or pushed")
            elif not res["pushed"]:
                print("  no changes to commit; nothing pushed")
            else:
                print(f"  committed {res['commit']} and pushed")
                if args.create_pr:
                    report["pull_request"] = create_pull_request(args.repo_url, res["branch"], args.base_branch, args.message,
                                                                 "Notebooks converted by adb2fabric. Review TODO/REVIEW markers before merging.",
                                                                 getattr(args, "ado_api_base", None))
                    print(f"  pull request: {report['pull_request']}")
    except (AuthError, ApiError, GitError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        report["error"] = str(e)
        code = USAGE if isinstance(e, AuthError) else FAILED
    rdir = root / "_report"
    rdir.mkdir(exist_ok=True)
    out = rdir / f"publish_{args.target}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\n  report: {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
