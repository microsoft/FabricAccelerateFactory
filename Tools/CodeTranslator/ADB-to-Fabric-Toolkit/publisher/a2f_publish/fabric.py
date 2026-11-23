from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.fabric.microsoft.com/v1"
RESOURCE = "https://api.fabric.microsoft.com"


class AuthError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


def folder_parts(path: str) -> list[str]:
    if not path:
        return []
    parts = path.replace("/", "\\").split("\\")
    if len(parts) > 10:
        raise ApiError(400, "Fabric destination folder must have at most 10 levels.")
    for name in parts:
        if (not name or name != name.strip() or len(name) > 255
                or name.casefold() in {"$recycle.bin", "recycled", "recycler"}
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '~"#.&*:<>?/{|}' for c in name)):
            raise ApiError(400, f"Invalid Fabric folder name: {name!r}. Use a relative folder path with valid names.")
    return parts


def get_token() -> str:
    """FABRIC_TOKEN env var, else the signed-in Azure CLI session. The token is never printed or stored."""
    tok = os.environ.get("FABRIC_TOKEN", "").strip()
    if tok:
        return tok
    az = shutil.which("az")
    if not az:
        raise AuthError("No credentials: set FABRIC_TOKEN, or install Azure CLI and run 'az login'.")
    r = subprocess.run([az, "account", "get-access-token", "--resource", RESOURCE, "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise AuthError("Azure CLI could not get a Fabric token. Run 'az login' (use an account with access to the workspace).")
    return r.stdout.strip()


class Client:
    def __init__(self, base=API_BASE, token_fn=get_token, sleep=time.sleep, max_retries=5, poll_timeout=900):
        self.base, self.token_fn, self.sleep = base.rstrip("/"), token_fn, sleep
        self.max_retries, self.poll_timeout = max_retries, poll_timeout
        self._token = None

    def _request(self, method, url, body=None):
        for attempt in range(self.max_retries + 1):
            if self._token is None:
                self._token = self.token_fn()
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as e:
                payload = e.read().decode("utf-8", "replace")[:600]
                hdr = {k.lower(): v for k, v in e.headers.items()}
                if e.code == 401:
                    raise AuthError("401 from Fabric: token expired or the account has no access to this workspace.")
                if (e.code == 429 or e.code in (502, 503, 504)) and attempt < self.max_retries:
                    self.sleep(float(hdr.get("retry-after", 2 ** attempt)))
                    continue
                raise ApiError(e.code, payload)
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    self.sleep(2 ** attempt)
                    continue
                raise ApiError(0, f"network error: {e.reason}")
        raise ApiError(0, "retries exhausted")

    def _wait(self, headers):
        """Poll a long-running operation (202 + Location) until it finishes."""
        loc = headers.get("location")
        if not loc:
            return
        waited = 0.0
        while waited <= self.poll_timeout:
            delay = float(headers.get("retry-after", 2))
            self.sleep(delay)
            waited += delay
            _, headers, data = self._request("GET", loc)
            state = (json.loads(data or b"{}") or {}).get("status")
            if state in (None, "Succeeded"):
                return
            if state in ("Failed", "Cancelled"):
                err = json.loads(data).get("error") or {}
                raise ApiError(0, f"operation {state}: {err.get('message', '')}")
        raise ApiError(0, "timed out waiting for the operation")

    def workspace_id_by_name(self, name):
        _, _, data = self._request("GET", f"{self.base}/workspaces")
        hits = [w["id"] for w in json.loads(data).get("value", []) if w.get("displayName") == name]
        if len(hits) != 1:
            raise ApiError(404, f"workspace '{name}' not found or ambiguous ({len(hits)} matches)")
        return hits[0]

    def list_notebooks(self, ws) -> dict[str, str]:
        out, url = {}, f"{self.base}/workspaces/{ws}/notebooks"
        token = None
        while True:
            _, _, data = self._request("GET", url + (f"?continuationToken={token}" if token else ""))
            j = json.loads(data)
            out.update({n["displayName"]: n["id"] for n in j.get("value", [])})
            token = j.get("continuationToken")
            if not token:
                return out

    def list_folders(self, ws):
        folders, token = [], None
        while True:
            query = {"recursive": "true"}
            if token:
                query["continuationToken"] = token
            _, _, data = self._request("GET", f"{self.base}/workspaces/{ws}/folders?{urllib.parse.urlencode(query)}")
            page = json.loads(data)
            folders.extend(page.get("value", []))
            token = page.get("continuationToken")
            if not token:
                return folders

    def resolve_folder(self, ws, path, dry_run):
        parts = folder_parts(path)
        destination = {"path": "\\".join(parts), "id": None, "steps": []}
        if not parts:
            return destination
        folders = self.list_folders(ws)
        parent, missing = None, False
        for i, name in enumerate(parts):
            hits = [] if missing else [
                f for f in folders
                if f.get("parentFolderId") == parent and f.get("displayName", "").casefold() == name.casefold()
            ]
            if len(hits) > 1:
                raise ApiError(409, f"Ambiguous Fabric folder path: {' / '.join(parts[:i + 1])}")
            if hits:
                parent, action = hits[0]["id"], "reuse"
                if not parent:
                    raise ApiError(0, f"Existing folder '{name}' has no folder ID; publishing stopped.")
            elif dry_run:
                parent, missing, action = None, True, "would create"
            else:
                body = {"displayName": name}
                if parent:
                    body["parentFolderId"] = parent
                _, _, data = self._request("POST", f"{self.base}/workspaces/{ws}/folders", body)
                parent = json.loads(data).get("id")
                if not parent:
                    raise ApiError(0, f"Create folder '{name}' returned no folder ID; publishing stopped.")
                action = "created"
            destination["steps"].append({"path": "\\".join(parts[:i + 1]), "id": parent, "action": action})
        destination["id"] = parent
        return destination

    def move(self, ws, item_id, folder_id):
        url = f"{self.base}/workspaces/{ws}/items/{item_id}"
        _, _, data = self._request("GET", url)
        if json.loads(data).get("folderId") == folder_id:
            return
        _, _, data = self._request("POST", url + "/move", {"targetFolderId": folder_id})
        moved = json.loads(data).get("value", [])
        if not any(item.get("id") == item_id and item.get("folderId") == folder_id for item in moved):
            raise ApiError(0, "Move response did not confirm the requested notebook destination folder.")

    @staticmethod
    def _definition(raw: bytes):
        # Per Microsoft Learn (Notebook definition): single ipynb part, path must be exactly artifact.content.ipynb
        return {"format": "ipynb", "parts": [{"path": "artifact.content.ipynb",
                                              "payload": base64.b64encode(raw).decode("ascii"), "payloadType": "InlineBase64"}]}

    def create(self, ws, name, raw, description="", folder_id=None):
        body = {"displayName": name, "description": description[:256], "definition": self._definition(raw)}
        if folder_id:
            body["folderId"] = folder_id
        status, headers, _ = self._request("POST", f"{self.base}/workspaces/{ws}/notebooks", body)
        if status == 202:
            self._wait(headers)

    def update(self, ws, item_id, raw):
        status, headers, _ = self._request("POST", f"{self.base}/workspaces/{ws}/notebooks/{item_id}/updateDefinition",
                                           {"definition": self._definition(raw)})
        if status == 202:
            self._wait(headers)


def publish(client: Client, ws: str, nbs, on_exists: str, dry_run: bool, description: str, folder: str = ""):
    parts = folder_parts(folder)
    existing = client.list_notebooks(ws)
    actionable = any(n.name not in existing or on_exists == "update" for n in nbs)
    destination = client.resolve_folder(ws, "\\".join(parts), dry_run or not actionable)
    destination["used"] = actionable
    results = []
    for n in nbs:
        item_id = existing.get(n.name)
        action = "create" if item_id is None else {"skip": "skip", "update": "update", "fail": "fail"}[on_exists]
        r = {"notebook": n.rel, "name": n.name, "action": action, "result": "planned" if dry_run else "", "error": "", "warnings": []}
        r.update({"destination_folder": destination["path"], "folder_id": destination["id"],
                  "content_updated": False, "folder_placement": "unchanged"})
        if parts and action in ("create", "update"):
            r["folder_placement"] = "planned"
        if n.placeholders:
            r["warnings"].append(f"{n.placeholders} unresolved Key Vault placeholder(s)")
        if dry_run or action == "skip":
            r["result"] = "skipped (exists)" if action == "skip" else ("would fail (exists)" if action == "fail" else "planned")
        elif action == "fail":
            r["result"], r["error"] = "failed", "item already exists (use --on-exists update or skip)"
        else:
            try:
                raw = n.path.read_bytes()
                if action == "update":
                    client.update(ws, item_id, raw)
                    r["content_updated"] = True
                    if parts:
                        client.move(ws, item_id, destination["id"])
                else:
                    client.create(ws, n.name, raw, description, folder_id=destination["id"])
                if parts:
                    r["folder_placement"] = "in destination"
                elif action == "create":
                    r["folder_placement"] = "workspace root"
                r["result"] = "ok"
            except ApiError as e:
                r["result"], r["error"] = "failed", str(e)
                if parts:
                    r["folder_placement"] = "not confirmed"
                if r["content_updated"]:
                    r["error"] = "Notebook content was updated, but destination folder placement failed: " + str(e)
        results.append(r)
    return {"results": results, "destination_folder": destination}
