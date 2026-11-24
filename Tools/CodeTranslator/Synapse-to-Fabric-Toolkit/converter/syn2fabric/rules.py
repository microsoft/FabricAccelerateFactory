"""JSON-backed conversion rules and allowlisted Python replacement handlers.

AUTO is mechanical conversion; REVIEW requires verification; MANUAL leaves a TODO.
Silent rules add setup prerequisites without contributing to construct coverage.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .model import AUTO, MANUAL, RANK, REVIEW, Context

REGEX_FLAGS = {"IGNORECASE": re.I, "MULTILINE": re.M, "DOTALL": re.S, "ASCII": re.A}

# Runtime-context keys exposed by notebookutils.runtime.context in Fabric.
ENV_CONTEXT_KEYS = {
    "getWorkspaceName": "currentWorkspaceName",
    "getJobId": "activityId",
    "getClusterId": "clusterId",
    "getPoolName": "poolName",
    "getUserId": "userId",
    "getUserName": "userName",
}


def _vault_url(name: str, cfg) -> tuple[str, bool]:
    """Resolve a vault name to a Key Vault URL. The flag is True when the URL was configured or supplied explicitly."""
    if name.lower().startswith("https://"):
        return (name if name.endswith("/") else name + "/"), True
    configured = cfg.key_vaults.get(name)
    if configured:
        return configured, True
    return f"https://{name}.vault.azure.net/", False


def _key_vault_secret(m: re.Match, ctx: Context, cfg):
    vault = m.group("vault")
    url, configured = _vault_url(vault, cfg)
    ctx.need("keyvault", f"Key Vault '{vault}' ({url}): the identity running the notebook needs secret-read (Get) access.")
    note = " The linked-service argument was removed because Fabric has no linked services." if m.groupdict().get("ls") else ""
    message = (("Used the configured Key Vault URL." if configured else
                f"Vault URL derived from the name ('{url}'); verify it.") + note
               + " Grant the executing identity secret-read access; managed identity for Key Vault is not supported in notebooks.")
    return f'notebookutils.credentials.getSecret("{url}", {m.group("secret")})', AUTO, message


def _key_vault_secret_ls(m: re.Match, ctx: Context, cfg):
    linked_service = m.group("ls")
    url = cfg.linked_services.get(linked_service)
    if url:
        ctx.need("keyvault", f"Linked service '{linked_service}' -> {url}: grant the executing identity secret-read access.")
        return (f'notebookutils.credentials.getSecret("{url}", {m.group("secret")})', AUTO,
                "Linked service replaced by the configured Key Vault URL. Grant the executing identity secret-read access.")
    placeholder = f"<KEY_VAULT_URL_FOR_{linked_service}>"
    ctx.need("keyvault", f"Linked service '{linked_service}': set linked_services in the rules file or replace {placeholder} with the Key Vault URL.")
    return (f'notebookutils.credentials.getSecret("{placeholder}", {m.group("secret")})', AUTO,
            "Converted to the Fabric Key Vault API. The vault URL is a placeholder: set linked_services in the rules file "
            "(linked service -> vault URL) and re-run, then grant the executing identity secret-read access.")


def _run_notebook(m: re.Match, ctx: Context, cfg):
    target = m.group("target").strip("\"'")
    base = target.rstrip("/").rsplit("/", 1)[-1]
    mapped = cfg.notebook_mappings.get(target) or cfg.notebook_mappings.get(base)
    name = re.sub(r"\.(?:ipynb|json)$", "", mapped or base)
    shown = name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else json.dumps(name, ensure_ascii=False)
    ctx.need("run", f"%run {target}: notebook '{name}' must exist in the Fabric workspace under this name.")
    text = f'{m.group("indent")}%run {m.group("flags")}{shown}{m.group("rest")}'
    if mapped:
        return text, AUTO, (f"Converted using the configured notebook_mappings entry ({target} -> {name}). "
                            "Make sure the target notebook exists in the Fabric workspace.")
    return text, AUTO, (f"Folder path reduced to the notebook name '{name}' because Fabric references notebooks by name. "
                        "Make sure a notebook with this name exists in the Fabric workspace, or map it in notebook_mappings.")


def _env_context(m: re.Match, ctx: Context, cfg):
    key = ENV_CONTEXT_KEYS[m.group("method")]
    return f'notebookutils.runtime.context.get("{key}")', None, (
        f"Replaced mssparkutils.env.{m.group('method')}() with the Fabric runtime context key '{key}'. Values differ from Synapse; verify them.")


Handler = Callable[[re.Match, Context, object], tuple]
HANDLERS: dict[str, Handler] = {
    "key_vault_secret": _key_vault_secret,
    "key_vault_secret_ls": _key_vault_secret_ls,
    "run_notebook": _run_notebook,
    "env_context": _env_context,
}
HANDLER_GROUPS = {
    "key_vault_secret": {"vault", "secret", "ls"},
    "key_vault_secret_ls": {"ls", "secret"},
    "run_notebook": {"indent", "flags", "target", "rest"},
    "env_context": {"method"},
}


@dataclass
class Rule:
    id: str
    title: str
    pattern: str
    level: str
    scope: str = "py"
    sub: str | Handler | None = None
    message: str = ""
    checklist: tuple[str, str] | None = None
    silent: bool = False
    skip_on_manual: bool = False
    flags: int = 0
    rx: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        self.rx = re.compile(self.pattern, self.flags)

    def apply(self, text: str, ctx: Context, cfg) -> tuple[str, str, str]:
        """Returns (converted text, effective level, message)."""
        if self.level == MANUAL or self.sub is None:
            return text, self.level, self.message
        if not callable(self.sub):
            return self.rx.sub(self.sub, text), self.level, self.message
        outcomes = []

        def replace(m: re.Match) -> str:
            replacement, level, message = self.sub(m, ctx, cfg)
            outcomes.append((level or self.level, message or self.message))
            return replacement

        converted = self.rx.sub(replace, text)
        level = max((lv for lv, _ in outcomes), key=RANK.get, default=self.level)
        message = " ".join(dict.fromkeys(msg for _, msg in outcomes if msg)) or self.message
        return converted, level, message


def build_rules(data: dict) -> list[Rule]:
    rules = []
    for entry in data["rules"]:
        flags = 0
        for flag in entry.get("flags", []):
            flags |= REGEX_FLAGS[flag]
        checklist = entry.get("checklist")
        rules.append(Rule(
            id=entry["id"], title=entry["title"], pattern=entry["pattern"], level=entry["level"],
            scope=entry.get("scope", "py"),
            sub=HANDLERS[entry["handler"]] if "handler" in entry else entry.get("replacement"),
            message=entry.get("message", ""),
            checklist=(checklist[0], checklist[1]) if checklist else None,
            silent=entry.get("silent", False),
            skip_on_manual=entry.get("skip_on_manual", False),
            flags=flags,
        ))
    return rules
