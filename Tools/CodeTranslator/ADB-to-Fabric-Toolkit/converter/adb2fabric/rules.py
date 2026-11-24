"""JSON-backed conversion rules and allowlisted Python replacement handlers.

AUTO is mechanical conversion; REVIEW requires verification; MANUAL leaves a TODO.
Silent rules add setup prerequisites without contributing to construct coverage.
"""
from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass
from typing import Callable

from .config import CONFIG, REGEX_FLAGS
from .model import AUTO, MANUAL, REVIEW, Context, Item

Sub = Callable[[re.Match, Context], str]


@dataclass
class Rule:
    id: str
    title: str
    pattern: str
    level: str
    scope: str = "py"
    sub: str | Sub | None = None
    message: str = ""
    checklist: tuple[str, str] | None = None
    silent: bool = False
    flags: int = 0

    def __post_init__(self):
        self.rx = re.compile(self.pattern, self.flags)


def _ident(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _widget_define(m: re.Match, ctx: Context) -> str:
    name, default = m.group("name"), m.group("default")
    if not _ident(name):
        return m.group(0)
    ctx.params.setdefault(name, repr(CONFIG.get("parameter_defaults", {}).get(name, default)))
    return f'{m.group("indent")}pass  # [ADB2FABRIC] widget "{name}" is now a variable in the parameters cell'


def _widget_get(m: re.Match, ctx: Context) -> str:
    name = m.group("name")
    if not _ident(name):
        return m.group(0)
    defaults = CONFIG.get("parameter_defaults", {})
    ctx.params.setdefault(name, repr(defaults[name]) if name in defaults else '""')
    ctx.need("parameters", f"{name}: " + ("configured default; verify pipeline binding."
                                       if name in defaults else "supply a pipeline parameter or configure parameter_defaults."))
    return name


def _secret_arguments(call):
    if not isinstance(call, ast.Call) or len(call.args) > 2:
        return None
    names = [kw.arg for kw in call.keywords]
    if (any(name not in ("scope", "key") for name in names) or len(names) != len(set(names))
            or set(names) & set(("scope", "key")[:len(call.args)])
            or len(call.args) + len(names) != 2):
        return None
    values = dict(zip(("scope", "key"), call.args))
    values.update((kw.arg, kw.value) for kw in call.keywords)
    scope = values["scope"]
    if not isinstance(scope, ast.Constant) or not isinstance(scope.value, str):
        return None
    return scope.value, values["key"]


def _vault(scope):
    vault = CONFIG.get("secret_scopes", {}).get(scope)
    resolved = bool(vault and vault.rstrip("/").lower() != "https://your-vault.vault.azure.net")
    return vault or f"<KEY_VAULT_URL_FOR_{scope}>", resolved


def secret_conversion(expression, ctx):
    try:
        args = _secret_arguments(ast.parse(expression, mode="eval").body)
    except SyntaxError:
        args = None
    if args is None:
        message = "Dynamic scope or unsupported secret arguments: map the scope and migrate this call manually."
        ctx.need("keyvault", message)
        return expression, MANUAL, message
    scope, key = args
    vault, resolved = _vault(scope)
    message = ("Used configured scope-to-Key-Vault URL. Grant the Fabric execution identity access; no credentials are stored in configuration."
               if resolved else "API converted to Fabric getSecret; vault URL remains a migration dependency. Replace the placeholder using secret_scopes and grant the execution identity access before running.")
    ctx.need("keyvault", f"scope {scope!r}: {vault}; " + (
        "grant execution identity secret-read access." if resolved else "replace placeholder with the actual vault URL before running."))
    return f"notebookutils.credentials.getSecret({vault!r}, {ast.unparse(key)})", AUTO if resolved else REVIEW, message


def _secret(m: re.Match, ctx: Context) -> str:
    return secret_conversion(m.group(0), ctx)[0]


def convert_secrets(lines, ctx, file, cell_idx, start_line):
    """Rewrite actual Python calls, including multiline calls, without touching strings/comments."""
    source = "\n".join(lines)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines, {}, False
    evidence = {}
    edits = []
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def offset(line, column):
        return starts[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) == "dbutils.secrets.get"):
            continue
        start, end = offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)
        original = source[start:end]
        replacement, level, message = secret_conversion(original, ctx)
        evidence.setdefault(node.lineno - 1, []).append(
            Item(file, cell_idx, start_line + node.lineno - 1, "S1", level,
                 "dbutils.secrets.get -> Key Vault getSecret", original, replacement, message))
        if level != MANUAL:
            gap = node.end_lineno - node.lineno
            edits.append((start, end, "(" + replacement + "\n" * gap + ")" if gap else replacement))
    # Nested secret lookups cannot be edited independently without corrupting their outer call.
    if any(a < c < b for a, b, _ in edits for c, _, _ in edits):
        for findings in evidence.values():
            for item in findings:
                item.level, item.converted = MANUAL, item.original
                item.message = "Nested secret lookups require manual conversion."
        return lines, evidence, True
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source.split("\n"), evidence, True


HANDLERS: dict[str, Sub] = {
    "widget_define": _widget_define,
    "widget_get": _widget_get,
    "secret": _secret,
}


def build_rules(config) -> list[Rule]:
    result = []
    for entry in config["rules"]:
        flags = 0
        for flag in entry.get("flags", []):
            flags |= REGEX_FLAGS[flag]
        checklist = entry.get("checklist")
        result.append(Rule(
            id=entry["id"], title=entry["title"], pattern=entry["pattern"], level=entry["level"],
            scope=entry.get("scope", "py"),
            sub=HANDLERS[entry["handler"]] if "handler" in entry else entry.get("replacement"),
            message=entry.get("message", ""),
            checklist=(checklist[0], checklist[1]) if checklist else None,
            silent=entry.get("silent", False), flags=flags,
        ))
    return result


RULES = build_rules(CONFIG)
MAGIC_MESSAGES = {entry["kind"]: (entry["id"], entry["level"], entry["title"])
                  for entry in CONFIG["magic_commands"]}
