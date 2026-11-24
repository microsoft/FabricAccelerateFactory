from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps Entra application id (az --resource)


class GitError(Exception):
    pass


def _env_with_pat(env=None):
    """Give git the PAT through environment config, never through the URL or argv (those leak into logs / process lists)."""
    e = dict(os.environ if env is None else env)
    pat = e.get("ADO_PAT", "").strip()
    if pat:
        hdr = base64.b64encode(f":{pat}".encode()).decode()
        e.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {hdr}"})
    e.setdefault("GIT_AUTHOR_NAME", "syn2fabric")
    e.setdefault("GIT_AUTHOR_EMAIL", "syn2fabric@users.noreply.invalid")
    e.setdefault("GIT_COMMITTER_NAME", e["GIT_AUTHOR_NAME"])
    e.setdefault("GIT_COMMITTER_EMAIL", e["GIT_AUTHOR_EMAIL"])
    return e


def _git(args, cwd=None, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=_env_with_pat(env), capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["git failed"]
        raise GitError(f"git {' '.join(a for a in args[:2])}: {msg[0]}")
    return r.stdout.strip()


def _rmtree(path):
    def onerror(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror, ignore_errors=False)


def parse_ado_url(url: str):
    """Returns (org, project, repo) for dev.azure.com or *.visualstudio.com URLs, else None."""
    m = re.match(r"https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)", url)
    if m:
        return tuple(re.sub(r"%20", " ", x) for x in m.groups())
    m = re.match(r"https://([^./]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/([^/?#]+)", url)
    return m.groups() if m else None


def _branch_name(name: str | None) -> str:
    return re.sub(r"[^\w./-]", "-", name) if name else "syn2fabric/" + datetime.now().strftime("%Y%m%d-%H%M%S")


def push_to_repo(nbs, repo_url, base_branch, branch, folder, message, dry_run):
    """Clone, add the notebooks on a NEW branch, commit and push. Additive only: nothing in the repo is deleted."""
    branch = _branch_name(branch)
    folder = folder.strip("/\\")
    result = {"branch": branch, "base": base_branch, "folder": folder, "files": [], "pushed": False, "changes": 0, "commit": ""}
    tmp = Path(tempfile.mkdtemp(prefix="s2f_ado_"))
    try:
        repo = tmp / "repo"
        _git(["clone", "--quiet", "--depth", "1", "--branch", base_branch, repo_url, str(repo)])
        _git(["checkout", "--quiet", "-b", branch], cwd=repo)
        manifest = []
        for n in nbs:
            dest = repo / folder / n.rel if folder else repo / n.rel
            same = dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == n.sha256
            result["files"].append({"notebook": n.rel, "repo_path": dest.relative_to(repo).as_posix(), "state": "unchanged" if same else ("modified" if dest.exists() else "new")})
            manifest.append({"notebook": n.rel, "sha256": n.sha256, "status": n.status})
            if not same:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(n.path, dest)
        mpath = repo / folder / "publish_manifest.json" if folder else repo / "publish_manifest.json"
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps({"tool": "s2f_publish", "notebooks": manifest}, indent=1), encoding="utf-8")
        _git(["add", "-A", "--", folder or "."], cwd=repo)
        result["changes"] = len([l for l in _git(["status", "--porcelain"], cwd=repo).splitlines() if l.strip()])
        if dry_run or result["changes"] == 0:
            return result
        _git(["commit", "--quiet", "-m", message], cwd=repo)
        result["commit"] = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
        _git(["push", "--quiet", "-u", "origin", branch], cwd=repo)
        result["pushed"] = True
        return result
    finally:
        try:
            _rmtree(tmp)
        except OSError:
            pass


def create_pull_request(repo_url, branch, base_branch, title, description, api_base=None, token_fn=None):
    """Open a PR via the Azure DevOps REST API. Auth: ADO_PAT env var, else the Azure CLI session."""
    parts = parse_ado_url(repo_url)
    if not parts:
        raise GitError("--create-pr needs a dev.azure.com or visualstudio.com repo URL")
    org, project, repo = parts
    base = (api_base or f"https://dev.azure.com/{org}").rstrip("/")
    url = f"{base}/{urllib.request.quote(project)}/_apis/git/repositories/{urllib.request.quote(repo)}/pullrequests?api-version=7.1"
    pat = os.environ.get("ADO_PAT", "").strip()
    if pat:
        auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
    else:
        tok = (token_fn or _az_ado_token)()
        auth = f"Bearer {tok}"
    body = {"sourceRefName": f"refs/heads/{branch}", "targetRefName": f"refs/heads/{base_branch}", "title": title, "description": description}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise GitError(f"pull request failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")
    return f"{base}/{urllib.request.quote(project)}/_git/{urllib.request.quote(repo)}/pullrequest/{j.get('pullRequestId')}"


def _az_ado_token():
    az = shutil.which("az")
    if not az:
        raise GitError("No credentials for the PR: set ADO_PAT or sign in with Azure CLI.")
    r = subprocess.run([az, "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise GitError("Azure CLI could not get an Azure DevOps token (run 'az login' or set ADO_PAT).")
    return r.stdout.strip()
