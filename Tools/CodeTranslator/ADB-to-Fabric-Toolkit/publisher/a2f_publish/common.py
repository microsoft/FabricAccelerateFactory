from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

TODO_MARK = "TODO[ADB2FABRIC]"
PLACEHOLDER = "<KEY_VAULT_URL_FOR_"
NEEDS_MANUAL = "Needs manual work"


@dataclass
class Nb:
    rel: str                 # path relative to the input folder, forward slashes
    path: Path
    status: str              # Converted | Converted - review | Needs manual work | Invalid
    todo: int = 0            # unresolved TODO[ADB2FABRIC] markers
    placeholders: int = 0    # unresolved <KEY_VAULT_URL_FOR_...> placeholders
    error: str = ""
    name: str = ""
    sha256: str = ""


def _text(nb: dict) -> str:
    """Code-cell text only: the converter's own markdown summary mentions the marker text and must not be counted."""
    return "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []) if c.get("cell_type") == "code")


def discover(root: Path) -> list[Nb]:
    """Find converted notebooks (*.ipynb) under root, validate them and read their conversion status."""
    status_by_target: dict[str, str] = {}
    report = root / "_report" / "report.json"
    if report.is_file():
        try:
            for n in json.loads(report.read_text(encoding="utf-8")).get("notebooks", []):
                if n.get("target"):
                    status_by_target[n["target"]] = n.get("status", "")
        except (OSError, ValueError):
            pass
    found: list[Nb] = []
    for p in sorted(root.rglob("*.ipynb")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("_report/") or "/.ipynb_checkpoints/" in "/" + rel:
            continue
        raw = p.read_bytes()
        n = Nb(rel, p, "Invalid", sha256=hashlib.sha256(raw).hexdigest())
        try:
            nb = json.loads(raw.decode("utf-8"))
            if nb.get("nbformat") != 4 or not isinstance(nb.get("cells"), list):
                raise ValueError("not an nbformat 4 notebook")
            text = _text(nb)
            n.todo, n.placeholders = text.count(TODO_MARK), text.count(PLACEHOLDER)
            n.status = status_by_target.get(rel) or (NEEDS_MANUAL if n.todo else "Converted")
            if n.todo and n.status != NEEDS_MANUAL:
                n.status = NEEDS_MANUAL          # trust the notebook content over a stale report
        except (ValueError, UnicodeDecodeError) as e:
            n.error = str(e)
        found.append(n)
    return found


def select(nbs: list[Nb], include_manual: bool):
    selected, blocked, invalid = [], [], []
    for n in nbs:
        if n.status == "Invalid":
            invalid.append(n)
        elif n.status == NEEDS_MANUAL and not include_manual:
            blocked.append(n)
        else:
            selected.append(n)
    return selected, blocked, invalid


def _clean(s: str) -> str:
    return re.sub(r"[^\w\- .]", "_", s).strip(" .")[:240] or "notebook"


def assign_names(nbs: list[Nb]) -> None:
    """Fabric item names are flat per workspace: use the file stem, and the folder path only where stems collide."""
    stems: dict[str, int] = {}
    for n in nbs:
        stems[_clean(Path(n.rel).stem)] = stems.get(_clean(Path(n.rel).stem), 0) + 1
    for n in nbs:
        stem = _clean(Path(n.rel).stem)
        n.name = stem if stems[stem] == 1 else _clean(n.rel.rsplit(".", 1)[0].replace("/", "__"))
