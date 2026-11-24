from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .rules import HANDLER_GROUPS, REGEX_FLAGS, Rule, build_rules

DEFAULT_CONFIG = Path(__file__).with_name("conversion-rules.json")
LEVELS = ("AUTO", "REVIEW", "MANUAL")
MAGIC_KINDS = {"code", "sql", "scala", "r", "csharp", "configure", "html", "passthrough", "unknown"}
IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


class ConfigError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _object(value, required, optional, location):
    _require(isinstance(value, dict), f"{location}: expected an object")
    missing, unknown = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    _require(not missing and not unknown,
             f"{location}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}")


def _string(value, location, allow_empty=False):
    _require(isinstance(value, str) and (allow_empty or bool(value.strip())), f"{location}: expected a string")


def _string_map(value, location):
    _require(isinstance(value, dict), f"{location}: expected an object")
    for key, target in value.items():
        _string(key, location)
        _string(target, f"{location}.{key}")


def _regex(entry, location, magic=False):
    _string(entry["pattern"], location + ".pattern")
    flags = entry.get("flags", [])
    _require(isinstance(flags, list) and set(flags) <= REGEX_FLAGS.keys(), f"{location}: unsupported regex flags")
    value = re.I if magic else 0
    for flag in flags:
        value |= REGEX_FLAGS[flag]
    try:
        rx = re.compile(entry["pattern"], value)
        if "replacement" in entry:
            _string(entry["replacement"], location + ".replacement", allow_empty=True)
            rx.sub(entry["replacement"], "")
    except (re.error, IndexError) as exc:
        raise ConfigError(f"{location}: invalid regex or replacement: {exc}") from exc
    return rx


def validate(data) -> None:
    required = {"schema_version", "target_platform", "key_vaults", "linked_services", "notebook_mappings", "table_mappings",
                "path_mappings", "magic_commands", "checklist_text", "cell_metadata", "cell_prefixes", "notebook_metadata",
                "excluded_rule_ids", "rules"}
    _object(data, required, {"description", "implementation_notes"}, "config")
    _require(data["schema_version"] == 1 and data["target_platform"] == "fabric", "config: unsupported schema_version or target_platform")
    for section in ("key_vaults", "linked_services"):
        _string_map(data[section], section)
        for key, url in data[section].items():
            _require(url.lower().startswith("https://"), f"{section}.{key}: expected an https:// Key Vault URL")
    _string_map(data["notebook_mappings"], "notebook_mappings")
    _string_map(data["table_mappings"], "table_mappings")
    sources = set()
    for source, target in data["table_mappings"].items():
        _require(re.fullmatch(IDENT + r"(?:\." + IDENT + r"){1,2}", source) is not None
                 and re.fullmatch(IDENT + r"(?:\." + IDENT + r"){0,2}", target) is not None,
                 "table_mappings: map a two- or three-part source identifier to a one-to-three-part target")
        _require(source.casefold() not in sources, f"table_mappings: duplicate case-insensitive source {source}")
        sources.add(source.casefold())
    _require(isinstance(data["path_mappings"], list), "path_mappings: expected an array")
    ids, prefixes = set(), set()
    for index, mapping in enumerate(data["path_mappings"]):
        location = f"path_mappings[{index}]"
        _object(mapping, {"id", "source", "target"}, {"enabled"}, location)
        for key in ("id", "source", "target"):
            _string(mapping[key], f"{location}.{key}")
        _require(type(mapping.get("enabled", True)) is bool, f"{location}.enabled: expected a boolean")
        _require(mapping["id"] not in ids, f"{location}: duplicate mapping ID")
        ids.add(mapping["id"])
        if mapping.get("enabled", True):
            _require(mapping["source"] not in prefixes, f"{location}: duplicate enabled source")
            prefixes.add(mapping["source"])
    _string_map(data["checklist_text"], "checklist_text")
    _object(data["cell_metadata"], {"code", "sql", "scala", "r"}, set(), "cell_metadata")
    for kind, pair in data["cell_metadata"].items():
        _require(isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair),
                 f"cell_metadata.{kind}: expected [language, language_group]")
    _require(isinstance(data["cell_prefixes"], dict) and set(data["cell_prefixes"]) <= {"sql", "scala", "r"}
             and all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in data["cell_prefixes"].values()),
             "cell_prefixes: expected sql/scala/r string arrays")
    _require(isinstance(data["notebook_metadata"], dict), "notebook_metadata: expected an object")
    _require(isinstance(data["excluded_rule_ids"], list) and all(isinstance(x, str) for x in data["excluded_rule_ids"]),
             "excluded_rule_ids: expected a string array")
    magic = data["magic_commands"]
    _require(isinstance(magic, list) and bool(magic), "magic_commands: expected a non-empty array")
    kinds = set()
    for index, entry in enumerate(magic):
        location = f"magic_commands[{index}]"
        _object(entry, {"kind", "id", "level", "title"}, {"pattern", "message"}, location)
        _require(entry["kind"] in MAGIC_KINDS, f"{location}: unknown kind {entry['kind']}")
        _require(entry["level"] in LEVELS, f"{location}: invalid level")
        _string(entry["id"], location + ".id")
        _string(entry["title"], location + ".title")
        if entry["kind"] == "unknown":
            _require("pattern" not in entry, f"{location}: the unknown entry must not have a pattern")
        else:
            _regex(entry, location, magic=True)
        kinds.add(entry["kind"])
    _require("unknown" in kinds and magic[-1]["kind"] == "unknown", "magic_commands: the unknown entry must be last")
    rules = data["rules"]
    _require(isinstance(rules, list) and bool(rules), "rules: expected a non-empty array")
    seen = set()
    for index, rule in enumerate(rules):
        location = f"rules[{index}]"
        _object(rule, {"id", "title", "pattern", "level"},
                {"scope", "replacement", "handler", "message", "checklist", "silent", "flags", "skip_on_manual"}, location)
        _string(rule["id"], location + ".id")
        _string(rule["title"], location + ".title")
        _require(rule["id"] not in seen, f"{location}: duplicate rule ID {rule['id']}")
        seen.add(rule["id"])
        _require(rule["level"] in LEVELS, f"{location}: invalid level")
        _require(rule.get("scope", "py") in ("py", "sql", "any"), f"{location}: invalid scope")
        for key in ("silent", "skip_on_manual"):
            _require(type(rule.get(key, False)) is bool, f"{location}.{key}: expected a boolean")
        _require(not ("replacement" in rule and "handler" in rule), f"{location}: use replacement OR handler")
        _require(rule["level"] != "MANUAL" or not ({"replacement", "handler"} & rule.keys()),
                 f"{location}: MANUAL rules cannot perform replacements")
        if "message" in rule:
            _string(rule["message"], location + ".message", allow_empty=True)
        if "checklist" in rule:
            checklist = rule["checklist"]
            _require(isinstance(checklist, list) and len(checklist) == 2 and all(isinstance(x, str) for x in checklist),
                     f"{location}: checklist must be [key, detail]")
            _require(checklist[0] in data["checklist_text"], f"{location}: unknown checklist key {checklist[0]}")
        _require(not rule.get("silent") or "checklist" in rule, f"{location}: silent rules need a checklist")
        rx = _regex(rule, location)
        if "handler" in rule:
            _require(rule["handler"] in HANDLER_GROUPS, f"{location}: unknown handler {rule['handler']}")
            _require(HANDLER_GROUPS[rule["handler"]] <= rx.groupindex.keys(),
                     f"{location}: missing named groups for {rule['handler']}")


@dataclass
class Settings:
    """Validated conversion settings loaded from a rules file."""
    path: Path
    data: dict
    rules: list[Rule]
    magics: list
    table_mappings: list
    path_mappings: list

    @property
    def key_vaults(self) -> dict:
        return self.data["key_vaults"]

    @property
    def linked_services(self) -> dict:
        return self.data["linked_services"]

    @property
    def notebook_mappings(self) -> dict:
        return self.data["notebook_mappings"]

    @property
    def checklist_text(self) -> dict:
        return self.data["checklist_text"]

    @property
    def excluded(self) -> frozenset:
        return frozenset(self.data["excluded_rule_ids"])


def load_settings(path=None) -> Settings:
    path = Path(path) if path else DEFAULT_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read rules file {path}: {exc}") from exc
    validate(data)
    magics = [(re.compile(e["pattern"], re.I) if "pattern" in e else None, e) for e in data["magic_commands"]]
    tables = [(re.compile(r"(?<![\w.])" + re.escape(src) + r"(?!\w)", re.I), src, dst)
              for src, dst in data["table_mappings"].items()]
    paths = [m for m in data["path_mappings"] if m.get("enabled", True)]
    return Settings(path, data, build_rules(data), magics, tables, paths)
