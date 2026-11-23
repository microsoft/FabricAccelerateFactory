from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from .config import CONFIG
from .model import AUTO, Item

CALL = "dbutils.jobs.taskValues.set"


def helper_references(root: Path) -> set[str]:
    """Conservatively identify local modules/notebooks referenced as shared helpers."""
    references = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in (".py", ".sql") or "_report" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"(?:%run\s+|^\s*(?:from|import)\s+)([^\s;]+)", text, re.M):
            target = match.group(1).strip("'\"")
            target = target.replace("\\", "/").rsplit("/", 1)[-1]
            if target.endswith((".py", ".sql")):
                target = target.rsplit(".", 1)[0]
            references.update(part.casefold() for part in target.split(".") if part)
    return references


def convert_task_values(cells, source, ctx, standalone):
    settings = CONFIG.get("task_values", {})
    if not settings.get("enabled", False):
        return [], False
    candidates, trees = [], []
    for cell in cells:
        if cell.kind == "markdown":
            continue
        if cell.kind != "code" or any(line.lstrip().startswith(("%", "!")) for line in cell.lines):
            return [], False
        text = "\n".join(cell.lines)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [], False
        trees.append(tree)
        top_level = {id(stmt.value) for stmt in tree.body if isinstance(stmt, ast.Expr)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or ast.unparse(node.func) != CALL:
                continue
            if id(node) not in top_level or node.args or len(node.keywords) != 2:
                return [], False
            keywords = {kw.arg: kw.value for kw in node.keywords}
            if set(keywords) != {"key", "value"}:
                return [], False
            try:
                key, value = ast.literal_eval(keywords["key"]), ast.literal_eval(keywords["value"])
                if not isinstance(key, str) or not key or not _json_value(value):
                    return [], False
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError, SyntaxError):
                return [], False
            candidates.append((cell, node, key, ast.get_source_segment(text, keywords["value"])))
    if not candidates or not standalone:
        return [], False
    names = {node.id for tree in trees for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.name for tree in trees for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    if {"fabric_task_values", "_a2f_json"} & names:
        return [], False
    if any(isinstance(node, ast.Call) and (
        ast.unparse(node.func).endswith((".exit", ".exitAll", ".stop", ".get_ipython"))
        or ast.unparse(node.func) in {"exit", "quit", "exec", "eval", "get_ipython"}
    ) for tree in trees for node in ast.walk(tree)):
        return [], False
    # Avoid future imports being displaced by the generated initialization cell.
    if any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
           for tree in trees for node in ast.walk(tree)):
        return [], False
    items, edits = [], []
    for cell, node, key, value_source in reversed(candidates):
        original = "\n".join(cell.lines[node.lineno - 1:node.end_lineno])
        first, last = cell.lines[node.lineno - 1], cell.lines[node.end_lineno - 1]
        prefix = first.encode("utf-8")[:node.col_offset].decode("utf-8")
        suffix = last.encode("utf-8")[node.end_col_offset:].decode("utf-8")
        # A semicolon can combine statements; only rewrite a complete expression statement.
        if prefix.strip() or (suffix.strip() and not suffix.lstrip().startswith("#")):
            return [], False
        replacement = f"fabric_task_values[{key!r}] = {value_source}"
        candidates_lines = replacement.split("\n")
        rewritten = (
            [prefix + candidates_lines[0]] + candidates_lines[1:-1]
            + ([candidates_lines[-1] + suffix] if len(candidates_lines) > 1 else [])
            + [""] * (node.end_lineno - node.lineno + 1 - len(candidates_lines))
        )
        if len(candidates_lines) == 1:
            rewritten[0] += suffix
        edits.append((cell, node.lineno - 1, node.end_lineno, rewritten))
        items.append(Item(source, cell.index, cell.start_line + node.lineno - 1, "J1", AUTO,
                          "dbutils.jobs.taskValues.set -> Fabric notebook output",
                          original.strip(), replacement, settings["message"]))
    for cell, start, end, rewritten in edits:
        cell.lines[start:end] = rewritten
    ctx.need("task_values", settings["message"])
    return list(reversed(items)), True


def _json_value(value):
    if value is None or type(value) in (str, bool, int, float):
        return True
    if isinstance(value, list):
        return all(_json_value(entry) for entry in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(entry) for key, entry in value.items())
    return False
