from __future__ import annotations

import json
from dataclasses import dataclass, field

from .model import Cell

KERNEL_LANGUAGES = {
    "synapse_pyspark": "python",
    "synapse_spark": "scala",
    "synapse_sparkdotnet": "csharp",
    "synapse_sparkr": "r",
    "synapse_sql": "sql",
}
LANGUAGE_NAMES = {"python": "python", "pyspark": "python", "scala": "scala", "sql": "sql", "r": "r",
                  "csharp": "csharp", "c#": "csharp", "dotnet": "csharp"}


class NotebookSkipped(Exception):
    """The file is not a convertible Synapse notebook; the message is the reason shown in the report."""


@dataclass
class SynapseNotebook:
    language: str                              # default language of cells without a cell magic
    cells: list[Cell]
    pool: str = ""
    pool_detail: str = ""
    session_properties: dict = field(default_factory=dict)
    source_format: str = "ipynb"               # ipynb | workspace-json
    warnings: list[str] = field(default_factory=list)


def _lines(source) -> list[str]:
    text = "".join(source) if isinstance(source, list) else (source or "")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _pool(meta: dict, body: dict) -> tuple[str, str]:
    compute = meta.get("a365ComputeOptions")
    if isinstance(compute, dict) and compute.get("name"):
        parts = [f"Spark {compute['sparkVersion']}" if compute.get("sparkVersion") else "",
                 f"{compute['nodeCount']} nodes" if compute.get("nodeCount") else "",
                 f"{compute['cores']} cores" if compute.get("cores") else "",
                 f"{compute['memory']} GB" if compute.get("memory") else ""]
        return str(compute["name"]), ", ".join(p for p in parts if p)
    pool = body.get("bigDataPool")
    if isinstance(pool, dict) and pool.get("referenceName"):
        return str(pool["referenceName"]), ""
    return "", ""


def load_notebook(text: str, include_plain: bool = False) -> SynapseNotebook:
    """Parse a Synapse notebook (Studio .ipynb export or workspace/Git JSON artifact).

    Raises NotebookSkipped with a user-facing reason when the file cannot or should not be converted.
    """
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError as exc:
        raise NotebookSkipped(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise NotebookSkipped("not a notebook (the JSON root is not an object)")
    body, workspace_json = data, False
    if isinstance(data.get("properties"), dict) and isinstance(data["properties"].get("cells"), list):
        body, workspace_json = data["properties"], True
    if not isinstance(body.get("cells"), list):
        raise NotebookSkipped("not a Synapse notebook (no cells; possibly a pipeline, dataset, linked service or other artifact)")
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    if {"microsoft", "dependencies", "syn2fabric", "a2f"} & meta.keys():
        raise NotebookSkipped("already a Fabric notebook (Fabric metadata found)")
    kernel = (meta.get("kernelspec") or {}).get("name", "") if isinstance(meta.get("kernelspec"), dict) else ""
    is_synapse = (workspace_json or kernel.startswith("synapse_") or "a365ComputeOptions" in meta
                  or isinstance(body.get("bigDataPool"), dict))
    if not is_synapse and not include_plain:
        raise NotebookSkipped("no Synapse notebook metadata (kernelspec synapse_*); use --include-plain to convert generic notebooks")
    info = meta.get("language_info") if isinstance(meta.get("language_info"), dict) else {}
    language = (LANGUAGE_NAMES.get(str(info.get("name", "")).lower())
                or KERNEL_LANGUAGES.get(kernel) or "python")
    pool, detail = _pool(meta, body)
    session = body.get("sessionProperties") if isinstance(body.get("sessionProperties"), dict) else {}
    cells, warnings = [], []
    for index, raw in enumerate(body["cells"]):
        if not isinstance(raw, dict):
            continue
        ctype = raw.get("cell_type")
        if ctype not in ("code", "markdown"):
            warnings.append(f"cell {index + 1}: unsupported cell type {ctype!r} was skipped.")
            continue
        cell_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        tags = [t for t in cell_meta.get("tags", []) if isinstance(t, str)] if isinstance(cell_meta.get("tags"), list) else []
        cells.append(Cell(index, ctype, _lines(raw.get("source")), tags))
    return SynapseNotebook(language, cells, pool, detail, session,
                           "workspace-json" if workspace_json else "ipynb", warnings)
