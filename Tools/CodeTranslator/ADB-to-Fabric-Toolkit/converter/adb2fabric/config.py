from __future__ import annotations

import json
import keyword
import re
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name("conversion-rules.json")
REGEX_FLAGS = {"IGNORECASE": re.I, "MULTILINE": re.M, "DOTALL": re.S, "ASCII": re.A}
HANDLER_GROUPS = {
    "widget_define": {"indent", "name", "default"},
    "widget_get": {"name"},
    "secret": {"scope", "key"},
}
MAGIC_IDS = {
    "markdown": "MAG-MD", "sql": "MAG-SQL", "code": "MAG-PY", "scala": "MAG-SCALA",
    "r": "MAG-R", "sh": "MAG-SH", "run": "MAG-RUN", "fs": "MAG-FS",
    "pip": "MAG-PIP", "unknown_magic": "MAG-UNK",
}


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


def _strings(value, location, allow_empty=False):
    _require(isinstance(value, list) and (allow_empty or bool(value)), f"{location}: expected a string array")
    for entry in value:
        _string(entry, location)
    _require(len(value) == len(set(value)), f"{location}: duplicate values")


def _pattern(entry, location, magic=False):
    _string(entry["pattern"], location + ".pattern")
    flags = entry.get("flags", [])
    _strings(flags, location + ".flags", allow_empty=True)
    _require(set(flags) <= REGEX_FLAGS.keys(), f"{location}: unsupported regex flag")
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


def validate_config(data):
    required = {"schema_version", "target_platform", "rules", "magic_commands", "manual_cases", "checklist_text",
                "cell_metadata", "cell_prefixes", "notebook_metadata", "fs_magic", "excluded_rule_ids"}
    _object(data, required, {"description", "implementation_notes", "path_mappings", "task_values", "dlt",
                             "autoloader", "secret_scopes", "parameter_defaults", "notebook_mappings",
                             "table_mappings", "mlflow"}, "config")
    _require(isinstance(data["checklist_text"], dict), "checklist_text: expected an object")
    for section in ("notebook_mappings", "table_mappings"):
        mapping = data.get(section, {})
        _require(isinstance(mapping, dict), f"{section}: expected an object")
        sources, targets = set(), set()
        for source, target in mapping.items():
            _string(source, f"{section} source")
            _string(target, f"{section}.{source}")
            _require(not any(ord(c) < 32 or ord(c) == 127 for c in source + target)
                     and source == source.strip() and target == target.strip(),
                     f"{section}: leading/trailing whitespace and control characters are not allowed")
            if section == "notebook_mappings":
                _require(not any(c in target for c in "/\\") and target not in (".", ".."),
                         f"{section}: target must be a Fabric notebook item name, not a path")
            else:
                identifier = r"[A-Za-z_][A-Za-z0-9_]*"
                _require(re.fullmatch(identifier + r"(?:\." + identifier + r"){2}", source) is not None
                         and re.fullmatch(identifier + r"(?:\." + identifier + r"){0,2}", target) is not None,
                         "table_mappings: map a simple three-part source identifier to a one-to-three-part target")
                _require(source.casefold() not in sources, "table_mappings: duplicate case-insensitive source")
                _require(target.casefold() not in targets, "table_mappings: multiple sources cannot share a target")
                sources.add(source.casefold())
                targets.add(target.casefold())
    if "mlflow" in data:
        settings = data["mlflow"]
        _object(settings, {"use_fabric_default_tracking", "experiment_mappings"}, set(), "mlflow")
        _require(type(settings["use_fabric_default_tracking"]) is bool,
                 "mlflow.use_fabric_default_tracking must be boolean")
        _require(isinstance(settings["experiment_mappings"], dict), "mlflow.experiment_mappings must be an object")
        targets = set()
        for source, target in settings["experiment_mappings"].items():
            _string(source, "mlflow.experiment_mappings source")
            _string(target, f"mlflow.experiment_mappings.{source}")
            _require(source.startswith("/") and source == source.strip()
                     and target == target.strip() and target not in (".", "..")
                     and not any(c in target for c in "/\\")
                     and not any(ord(c) < 32 or ord(c) == 127 for c in source + target),
                     "mlflow.experiment_mappings: map an absolute Databricks path to a Fabric experiment name")
            _require(target.casefold() not in targets,
                     "mlflow.experiment_mappings: multiple paths cannot share a target")
            targets.add(target.casefold())
        _require("mlflow" in data["checklist_text"], "mlflow requires checklist_text.mlflow")
    for name in ("secret_scopes", "parameter_defaults"):
        mapping = data.get(name, {})
        _require(isinstance(mapping, dict), f"{name}: expected an object")
        for key, value in mapping.items():
            _string(key, f"{name} key")
            _string(value, f"{name}.{key}", allow_empty=name == "parameter_defaults")
            if name == "parameter_defaults":
                _require(key.isidentifier() and not keyword.iskeyword(key),
                         f"{name}.{key}: expected a Python parameter identifier")
            else:
                _require(re.fullmatch(r"https://[a-zA-Z0-9][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]\.vault\.azure\.net/?", value)
                         is not None, f"{name}.{key}: expected an Azure public-cloud Key Vault HTTPS URL only")
    if "autoloader" in data:
        settings = data["autoloader"]
        _object(settings, {"enabled", "schemas"}, {"schema_mode", "remove_checkpoint_resets"}, "autoloader")
        _require(type(settings["enabled"]) is bool, "autoloader.enabled must be boolean")
        _require(settings.get("schema_mode", "explicit") in ("explicit", "runtime_parquet"),
                 "autoloader.schema_mode must be explicit or runtime_parquet")
        _require(type(settings.get("remove_checkpoint_resets", False)) is bool,
                 "autoloader.remove_checkpoint_resets must be boolean")
        _require(isinstance(settings["schemas"], dict), "autoloader.schemas must be an object")
        for notebook, readers in settings["schemas"].items():
            _string(notebook, "autoloader.schemas notebook")
            _require(notebook.endswith(".py") and "\\" not in notebook and not notebook.startswith("/")
                     and ":" not in notebook and all(p not in ("", ".", "..") for p in notebook.split("/")),
                     "autoloader.schemas: use an input-relative .py notebook path with / separators")
            _require(isinstance(readers, dict), f"autoloader.schemas.{notebook}: expected reader variable -> Spark DDL")
            for variable, schema in readers.items():
                _require(variable.isidentifier() and not keyword.iskeyword(variable),
                         f"autoloader.schemas.{notebook}: invalid reader variable")
                if schema is not None:
                    _string(schema, f"autoloader.schemas.{notebook}.{variable}")
                    _require(not any(ord(c) < 32 for c in schema),
                             "autoloader schema: use a single-line Spark DDL string; null means unresolved")
    if "dlt" in data:
        settings = data["dlt"]
        _object(settings, {"enabled", "target_mappings", "source_mappings", "replace_existing"}, set(), "dlt")
        for key in ("enabled", "replace_existing"):
            _require(type(settings[key]) is bool, f"dlt.{key} must be boolean")
        for section in ("target_mappings", "source_mappings"):
            mapping = settings[section]
            _require(isinstance(mapping, dict), f"dlt.{section} must be an object")
            keys, targets = set(), set()
            for key, value in mapping.items():
                _string(key, f"dlt.{section} key")
                _string(value, f"dlt.{section}.{key}")
                pattern = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
                _require(re.fullmatch(pattern, key) is not None and re.fullmatch(pattern, value) is not None,
                         f"dlt.{section}: use simple dot-qualified SQL identifiers without quoting")
                _require(key.casefold() not in keys, f"dlt.{section}: duplicate case-insensitive key")
                keys.add(key.casefold())
                if section == "target_mappings":
                    _require("." not in key and "." in value and len(value.split(".")) <= 4,
                             "dlt.target_mappings: map a dataset name to a schema-qualified Fabric target")
                    _require(value.casefold() not in targets, "dlt.target_mappings: multiple datasets cannot share a target")
                    targets.add(value.casefold())
        _require("dlt" in data["checklist_text"], "dlt requires checklist_text.dlt")
    if "task_values" in data:
        settings = data["task_values"]
        _object(settings, {"enabled", "message"}, set(), "task_values")
        _require(type(settings["enabled"]) is bool, "task_values.enabled must be boolean")
        _string(settings["message"], "task_values.message")
        _require("task_values" in data["checklist_text"], "task_values requires a checklist_text.task_values message")
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1, "schema_version must be 1")
    _require(data["target_platform"] == "fabric", "Only the fabric target is supported; this is not a Synapse profile")
    mappings = data.get("path_mappings", [])
    _require(isinstance(mappings, list), "path_mappings: expected an array")
    mapping_ids, sources = set(), set()
    for index, mapping in enumerate(mappings):
        loc = f"path_mappings[{index}]"
        _object(mapping, {"id", "source", "target"}, {"enabled", "scope"}, loc)
        for key in ("id", "source", "target"):
            _string(mapping[key], f"{loc}.{key}")
        _require(type(mapping.get("enabled", True)) is bool, f"{loc}: enabled must be boolean")
        _require(mapping.get("scope", "all") in ("all", "dbutils_fs"),
                 f"{loc}.scope: expected all or dbutils_fs")
        _require(mapping["id"] not in mapping_ids, f"{loc}: duplicate mapping ID")
        mapping_ids.add(mapping["id"])
        for key in ("source", "target"):
            value = mapping[key]
            _require(not any((c.isspace() and c != " ") or c in "'\"`\\{}"
                             or ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
                     f"{loc}.{key}: quotes, backslashes, braces and control characters are not supported")
            _require(value == value.strip(), f"{loc}.{key}: leading/trailing whitespace is not allowed")
            _require(not any(segment in (".", "..") for segment in value.split("/")),
                     f"{loc}.{key}: relative traversal segments are not allowed")
        source, target = mapping["source"].rstrip("/"), mapping["target"].rstrip("/")
        temporary = source == "/tmp" or source.startswith("/tmp/")
        _require(mapping["source"].startswith(("dbfs:/", "/dbfs/", "/mnt/", "/Volumes/", "/FileStore/", "abfss://"))
                 or (temporary and mapping.get("scope") == "dbutils_fs"),
                 f"{loc}.source: expected a DBFS, mount, volume or abfss prefix; /tmp requires dbutils_fs scope")
        _require(target.startswith(("abfss://", "Files/", "Tables/", "/lakehouse/default/Files/", "/lakehouse/default/Tables/")),
                 f"{loc}.target: expected abfss:// or a default-lakehouse Files/ or Tables/ path")
        if target.startswith("abfss://"):
            _require(re.fullmatch(r"abfss://[^/@<> ]+@[^/<> ]+/.+", target) is not None,
                     f"{loc}.target: use a complete abfss URI with real workspace/container and lakehouse/path values")
        if mapping.get("enabled", True):
            _require(source not in sources, f"{loc}: duplicate enabled source prefix")
            sources.add(source)
    if "description" in data:
        _string(data["description"], "description")
    for section in ("checklist_text", "implementation_notes"):
        if section not in data:
            continue
        _require(isinstance(data[section], dict), f"{section}: expected an object")
        for key, value in data[section].items():
            _string(key, section)
            _string(value, f"{section}.{key}")
    _require(set(data["checklist_text"]) >= {"library", "run", "keyvault"}, "checklist_text: missing built-in handler messages")
    rules = data["rules"]
    _require(isinstance(rules, list) and bool(rules), "rules: expected a non-empty array")
    ids = set()
    for index, rule in enumerate(rules):
        loc = f"rules[{index}]"
        _object(rule, {"id", "title", "pattern", "level"},
                {"scope", "replacement", "handler", "message", "checklist", "silent", "flags"}, loc)
        for key in ("id", "title"):
            _string(rule[key], f"{loc}.{key}")
        _require(rule["id"] not in ids, f"{loc}: duplicate rule ID {rule['id']}")
        ids.add(rule["id"])
        _require(rule["level"] in ("AUTO", "REVIEW", "MANUAL"), f"{loc}: invalid level")
        _require(rule.get("scope", "py") in ("py", "sql", "any"), f"{loc}: invalid scope")
        _require(type(rule.get("silent", False)) is bool, f"{loc}: silent must be boolean")
        _require(not ("replacement" in rule and "handler" in rule), f"{loc}: use replacement OR handler")
        _require(rule["level"] != "MANUAL" or not ({"replacement", "handler"} & rule.keys()),
                 f"{loc}: MANUAL rules cannot perform replacements")
        if "message" in rule:
            _string(rule["message"], loc + ".message", allow_empty=True)
        if "checklist" in rule:
            checklist = rule["checklist"]
            _require(isinstance(checklist, list) and len(checklist) == 2, f"{loc}: checklist must be [key, detail]")
            for entry in checklist:
                _string(entry, loc + ".checklist")
            _require(checklist[0] in data["checklist_text"], f"{loc}: unknown checklist key {checklist[0]}")
        _require(not rule.get("silent") or "checklist" in rule, f"{loc}: silent rules need a checklist")
        rx = _pattern(rule, loc)
        if "handler" in rule:
            handler = rule["handler"]
            _string(handler, loc + ".handler")
            _require(handler in HANDLER_GROUPS, f"{loc}: unknown handler {handler}")
            _require(HANDLER_GROUPS[handler] <= rx.groupindex.keys(), f"{loc}: missing named groups for {handler}")
    _require(rules[-1]["id"] == "Z9" and rules[-1]["level"] == "MANUAL"
             and rules[-1].get("scope") == "any" and not rules[-1].get("silent"),
             "rules: the MANUAL, any-scope Z9 catch-all must be last")
    _require("P-MAP" not in ids, "P-MAP is reserved for configured path mapping results")
    _require(not ids.intersection({"T-UC", "ML1", "ML2", "P-TMP", "F-MV", "P-CELL"}),
             "T-UC, ML1, ML2, P-TMP, F-MV and P-CELL are reserved for semantic migration results")
    _require(not any(rule_id.startswith("X3-") for rule_id in ids), "X3-* IDs are reserved for semantic DLT conversion results")

    magic = data["magic_commands"]
    _require(isinstance(magic, list), "magic_commands: expected an array")
    kinds = set()
    for index, entry in enumerate(magic):
        loc = f"magic_commands[{index}]"
        _object(entry, {"kind", "id", "level", "title"}, {"pattern"}, loc)
        kind = entry["kind"]
        _string(kind, loc + ".kind")
        _require(kind in MAGIC_IDS and kind not in kinds, f"{loc}: unsupported or duplicate kind")
        kinds.add(kind)
        _require(entry["id"] == MAGIC_IDS[kind], f"{loc}: built-in magic IDs must remain unchanged")
        _require(entry["id"] not in ids, f"{loc}: duplicate rule ID")
        ids.add(entry["id"])
        _require(entry["level"] in ("AUTO", "REVIEW", "MANUAL"), f"{loc}: invalid level")
        _string(entry["title"], loc + ".title")
        _require(("pattern" in entry) == (kind != "unknown_magic"), f"{loc}: pattern required except for unknown_magic")
        if "pattern" in entry:
            _pattern(entry, loc, magic=True)
    _require(kinds == MAGIC_IDS.keys(), "magic_commands: must define all built-in kinds")
    cases = data["manual_cases"]
    _object(cases, {"run_with_parameters", "unsupported_fs"}, set(), "manual_cases")
    for key, rule_id in (("run_with_parameters", "MAG-RUNARGS"), ("unsupported_fs", "MAG-FS")):
        entry = cases[key]
        loc = f"manual_cases.{key}"
        _object(entry, {"id", "level", "title", "message"}, set(), loc)
        _require(entry["id"] == rule_id and entry["level"] == "MANUAL", f"{loc}: preserve the built-in ID and MANUAL level")
        _string(entry["title"], loc + ".title")
        _string(entry["message"], loc + ".message", allow_empty=True)
    _require("MAG-RUNARGS" not in ids, "rules: MAG-RUNARGS is reserved for the built-in manual case")
    ids.add("MAG-RUNARGS")
    _strings(data["excluded_rule_ids"], "excluded_rule_ids", allow_empty=True)
    _require(set(data["excluded_rule_ids"]) <= ids, "excluded_rule_ids: unknown rule ID")

    for section, keys, size in (("cell_metadata", {"code", "sql", "scala", "r"}, 2),
                                ("cell_prefixes", {"sql", "scala", "r"}, None)):
        _object(data[section], keys, set(), section)
        for key, value in data[section].items():
            _strings(value, f"{section}.{key}", allow_empty=size is None)
            _require(size is None or len(value) == size, f"{section}.{key}: expected {size} values")
    meta = data["notebook_metadata"]
    _object(meta, {"kernel_info", "kernelspec", "language_info", "microsoft"}, set(), "notebook_metadata")
    for section, keys in (("kernel_info", {"name"}), ("kernelspec", {"name", "display_name"}),
                          ("language_info", {"name"}), ("microsoft", {"language", "language_group"})):
        _object(meta[section], keys, set(), f"notebook_metadata.{section}")
        for key, value in meta[section].items():
            _string(value, f"notebook_metadata.{section}.{key}")
    fs = data["fs_magic"]
    _object(fs, {"api_prefix", "commands", "display_result_commands"}, set(), "fs_magic")
    _string(fs["api_prefix"], "fs_magic.api_prefix")
    _require(all(part.isidentifier() for part in fs["api_prefix"].split(".")), "fs_magic.api_prefix: expected a dotted Python name")
    _strings(fs["commands"], "fs_magic.commands")
    _require(all(cmd.isidentifier() for cmd in fs["commands"]), "fs_magic.commands: expected Python method names")
    _strings(fs["display_result_commands"], "fs_magic.display_result_commands", allow_empty=True)
    _require(set(fs["display_result_commands"]) <= set(fs["commands"]), "fs_magic: display commands must be supported commands")
    return data


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_config(path=DEFAULT_CONFIG):
    path = Path(path)
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ConfigError(f"Invalid conversion configuration '{path}': {exc}") from exc


CONFIG = load_config()
