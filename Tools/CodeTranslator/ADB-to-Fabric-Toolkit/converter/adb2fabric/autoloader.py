"""Guarded file-stream migration with explicit or opt-in POC runtime schemas."""
from __future__ import annotations

import ast
import copy

from .config import CONFIG
from .model import AUTO, REVIEW, Item
from .paths import map_paths


class UnsupportedStream(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise UnsupportedStream(reason)


def _chain(node):
    calls = []
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append((node.func.attr, node.args, node.keywords))
        node = node.func.value
    return node, list(reversed(calls))


def _literal(node, value):
    return isinstance(node, ast.Constant) and node.value == value


def _path(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return ""


def _reader(statement, schema):
    _require(isinstance(statement, ast.Assign) and len(statement.targets) == 1
             and isinstance(statement.targets[0], ast.Name), "Reader must be a standalone top-level variable assignment.")
    base, calls = _chain(statement.value)
    _require(isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
             and base.value.id == "spark" and base.attr == "readStream", "Expected spark.readStream reader.")
    _require(len(calls) in (3, 4) and [c[0] for c in calls] == ["format"] + ["option"] * (len(calls) - 2) + ["load"],
             "Supported reader requires format, cloudFiles.format, optional cloudFiles.schemaLocation and load only; other options require manual migration.")
    _require(all(not kw for _, _, kw in calls), "Named or expanded reader arguments are not supported.")
    _require(len(calls[0][1]) == 1 and _literal(calls[0][1][0], "cloudFiles")
             and len(calls[-1][1]) == 1 and bool(_path(calls[-1][1][0])), "Expected literal cloudFiles format and literal/f-string source path.")
    options = {}
    for _, args, _ in calls[1:-1]:
        _require(len(args) == 2 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str),
                 "Reader options must have literal names and exactly two arguments.")
        _require(args[0].value not in options, "Duplicate Auto Loader options require manual migration.")
        options[args[0].value] = args[1]
    _require(set(options) in ({"cloudFiles.format"}, {"cloudFiles.format", "cloudFiles.schemaLocation"})
             and _literal(options["cloudFiles.format"], "parquet")
             and ("cloudFiles.schemaLocation" not in options or bool(_path(options["cloudFiles.schemaLocation"]))),
             "Only Parquet with an optional literal/f-string schemaLocation is supported.")
    source = calls[-1][1][0]
    if schema is None:
        _require(all(isinstance(n, (ast.JoinedStr, ast.Constant, ast.FormattedValue, ast.Name, ast.Load))
                     for n in ast.walk(source)),
                 "Runtime schema inference requires a literal source or f-string with simple variable substitutions.")
        schema_node = ast.Attribute(
            value=ast.Call(func=ast.Attribute(
                value=ast.Attribute(value=ast.Name(id="spark", ctx=ast.Load()), attr="read", ctx=ast.Load()),
                attr="parquet", ctx=ast.Load()), args=[copy.deepcopy(source)], keywords=[]),
            attr="schema", ctx=ast.Load())
    else:
        schema_node = ast.Constant(schema)
    load = ast.Call(
        func=ast.Attribute(value=ast.Call(
            func=ast.Attribute(value=ast.Call(
                func=ast.Attribute(value=copy.deepcopy(base), attr="format", ctx=ast.Load()),
                args=[ast.Constant("parquet")], keywords=[]),
                attr="schema", ctx=ast.Load()),
            args=[schema_node], keywords=[]), attr="load", ctx=ast.Load()),
        args=copy.deepcopy(calls[-1][1]), keywords=[])
    result = copy.deepcopy(statement)
    result.value = load
    return ast.unparse(ast.fix_missing_locations(result))


def _writer(statement, variable, lines):
    _require(isinstance(statement, ast.Expr), "Sink must be a standalone top-level expression.")
    base, calls = _chain(statement.value)
    _require(isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
             and base.value.id == variable and base.attr == "writeStream", "Expected a matching writeStream sink.")
    _require([c[0] for c in calls] == ["format", "outputMode", "option", "trigger", "toTable"],
             "Supported sink requires Delta append, one checkpointLocation, availableNow and toTable.")
    _require(all(not kw for _, _, kw in calls[:3] + calls[4:]), "Unsupported sink keyword arguments.")
    _require(len(calls[0][1]) == 1 and _literal(calls[0][1][0], "delta")
             and len(calls[1][1]) == 1 and _literal(calls[1][1][0], "append"),
             "Only Delta append sinks are supported.")
    _require(len(calls[2][1]) == 2 and _literal(calls[2][1][0], "checkpointLocation"),
             "A durable checkpointLocation is required.")
    _require(not calls[3][1] and len(calls[3][2]) == 1 and calls[3][2][0].arg == "availableNow"
             and isinstance(calls[3][2][0].value, ast.Constant) and calls[3][2][0].value.value is True,
             "Only availableNow=True is supported; continuous streams require manual orchestration.")
    _require(len(calls[4][1]) == 1 and bool(_path(calls[4][1][0])), "Expected literal/f-string target table.")
    mapped, _ = map_paths(lines[statement.lineno - 1:statement.end_lineno], "code")
    mapped_statement = ast.parse("\n".join(mapped)).body[0]
    _, mapped_calls = _chain(mapped_statement.value)
    checkpoint = _path(mapped_calls[2][1][1])
    _require(checkpoint.startswith(("Files/", "/lakehouse/default/Files/", "abfss://")),
             "Map checkpointLocation to a durable Fabric Files/OneLake/abfss path before converting.")
    return ast.unparse(statement.value)


def _path_key(node):
    mapped, _ = map_paths([ast.unparse(node)], "code")
    return ast.dump(ast.parse(mapped[0], mode="eval").body, include_attributes=False)


def _isolated(cell, statement, parsed):
    _require(not any(
        other_cell is cell and other is not statement
        and other.lineno <= statement.end_lineno and other.end_lineno >= statement.lineno
        for other_cell, other in parsed),
        "Stream/reset statements sharing source lines with other statements require manual migration.")


def _reset_plans(parsed, paths, enabled):
    plans = []
    for cell, statement in parsed:
        deletes = [n for n in ast.walk(statement) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and ast.unparse(n.func) in ("dbutils.fs.rm", "notebookutils.fs.rm")]
        if not deletes:
            continue
        _require(enabled, "Notebook deletes filesystem paths; enable remove_checkpoint_resets only after planning replay/deduplication.")
        _require(isinstance(statement, ast.Expr) and len(deletes) == 1 and statement.value is deletes[0],
                 "Conditional, assigned or nested filesystem deletion requires manual migration.")
        call = deletes[0]
        _require(1 <= len(call.args) <= 2 and not any(isinstance(a, ast.Starred) for a in call.args)
                 and len(call.keywords) <= 1
                 and (not call.keywords or (len(call.args) == 1 and call.keywords[0].arg == "recurse"))
                 and (len(call.args) == 1 or _literal(call.args[1], True))
                 and (not call.keywords or _literal(call.keywords[0].value, True)),
                 "Unsupported filesystem deletion arguments require manual migration.")
        _require(bool(_path(call.args[0])) and _path_key(call.args[0]) in paths,
                 "Filesystem deletion does not exactly match this stream's checkpoint/schema path; migrate manually.")
        _isolated(cell, statement, parsed)
        original = "\n".join(cell.lines[statement.lineno - 1:statement.end_lineno])
        plans.append((cell, statement,
                      'print("[ADB2FABRIC] Checkpoint/schema reset removed: retaining streaming progress; use a separate controlled reset for replay.")',
                      "X1-RESET", AUTO, original))
    return plans


def convert_autoloader(cells, file, ctx, standalone):
    candidates = [c for c in cells if c.kind == "code" and any("cloudFiles" in line for line in c.lines)]
    if not candidates:
        return [], {}
    settings = CONFIG.get("autoloader", {})
    plans = []
    try:
        _require(settings.get("enabled", False), "Auto Loader conversion is disabled; enable autoloader and configure an explicit source schema.")
        _require(standalone, "Auto Loader migration is limited to standalone Python notebook exports.")
        _require(all(c.kind in ("code", "markdown") for c in cells),
                 "Mixed-language/magic notebooks require manual stream migration.")
        parsed = []
        for cell in cells:
            if cell.kind != "code":
                continue
            try:
                tree = ast.parse("\n".join(cell.lines))
            except SyntaxError as exc:
                raise UnsupportedStream("Cannot safely analyse all Python cells: " + exc.msg) from exc
            parsed.extend((cell, statement) for statement in tree.body)
        used_names = {n.id for _, s in parsed for n in ast.walk(s) if isinstance(n, ast.Name)}
        schemas = settings.get("schemas", {}).get(file, {})
        infer_schema = settings.get("schema_mode", "explicit") == "runtime_parquet"
        missing = [s.targets[0].id for c, s in parsed if c in candidates and isinstance(s, ast.Assign)
                   and isinstance(s.targets[0], ast.Name) and "cloudFiles" in ast.unparse(s)
                   and not schemas.get(s.targets[0].id)]
        if missing and not infer_schema:
            ctx.need("autoloader", f"{file}: unresolved explicit schema for {', '.join(missing)}; set autoloader.schemas[notebook][reader] to real Spark DDL.")
        _require(not missing or infer_schema,
                 "Schema resolution remains a migration dependency: configure autoloader.schemas for " + ", ".join(missing) + ".")
        matched = set()
        reset_paths = set()
        for pos, (cell, statement) in enumerate(parsed):
            if cell not in candidates or "cloudFiles" not in ast.unparse(statement):
                continue
            _require(isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name),
                     "Auto Loader reader must be a top-level assignment.")
            variable = statement.targets[0].id
            schema = schemas.get(variable)
            _require(bool(schema) or infer_schema, f"Schema resolution remains a migration dependency: configure autoloader.schemas for {file}, reader {variable}.")
            replacement = _reader(statement, schema)
            sink = None
            for sink_cell, following in parsed[pos + 1:]:
                if any(isinstance(n, ast.Name) and n.id == variable for n in ast.walk(following)):
                    sink = (sink_cell, following)
                    break
            _require(sink is not None, "A matching availableNow Delta sink is required.")
            sink_cell, sink_statement = sink
            for selected_cell, selected in ((cell, statement), (sink_cell, sink_statement)):
                _isolated(selected_cell, selected, parsed)
            sink_code = _writer(sink_statement, variable, sink_cell.lines)
            _, reader_calls = _chain(statement.value)
            _, sink_calls = _chain(sink_statement.value)
            reset_paths.add(_path_key(sink_calls[2][1][1]))
            reset_paths.update(_path_key(args[1]) for method, args, _ in reader_calls
                               if method == "option" and _literal(args[0], "cloudFiles.schemaLocation"))
            query = f"_a2f_query_{cell.index}_{statement.lineno}"
            _require(query not in used_names, "Generated query name collides with existing code.")
            used_names.add(query)
            original = "\n".join(cell.lines[statement.lineno - 1:statement.end_lineno])
            sink_original = "\n".join(sink_cell.lines[sink_statement.lineno - 1:sink_statement.end_lineno])
            plans.append((cell, statement, replacement, "X1-READER", REVIEW, original))
            if not schema:
                ctx.need("autoloader", f"{file}, {variable}: POC runtime schema inference reads existing source Parquet metadata on each run; validate and version the schema before production. Missing/empty/inaccessible sources fail explicitly in Spark.")
            plans.append((sink_cell, sink_statement, f"{query} = {sink_code}; {query}.awaitTermination()",
                          "X1-WAIT", AUTO, sink_original))
            matched.add(cell.index)
        _require(matched == {c.index for c in candidates}, "Not all cloudFiles cells match the supported reader pattern.")
        reset_plans = _reset_plans(parsed, reset_paths, settings.get("remove_checkpoint_resets", False))
        plans.extend(reset_plans)
        if reset_plans:
            for cell, statement in parsed:
                if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                        and isinstance(statement.value.func, ast.Name) and statement.value.func.id == "print"
                        and len(statement.value.args) == 1 and not statement.value.keywords
                        and isinstance(statement.value.args[0], ast.JoinedStr)
                        and _path(statement.value.args[0]) == "Checkpoints cleared for: "):
                    _isolated(cell, statement, parsed)
                    plans.append((cell, statement, 'print("[ADB2FABRIC] Checkpoints preserved; automatic reset is disabled.")',
                                  "X1-RESET-LOG", AUTO, "\n".join(cell.lines[statement.lineno - 1:statement.end_lineno])))
    except UnsupportedStream as exc:
        reason = str(exc)
        ctx.need("autoloader", reason)
        return [], {c.index: reason for c in candidates}

    items = []
    for cell, statement, replacement, rule, level, original in plans:
        start, end = statement.lineno - 1, statement.end_lineno
        comment = "# REVIEW[ADB2FABRIC] X1-READER: validate discovery, schema and replay semantics" if level == REVIEW else ""
        # Preserve source-line offsets for the later path/rule/report pass.
        if comment:
            replacement += "  " + comment
        cell.lines[start:end] = [replacement] + [""] * (end - start - 1)
        converted, _ = map_paths([replacement], "code")
        title, message = {
            "X1-READER": ("Auto Loader -> schema-bound Parquet stream",
                          "cloudFiles replaced with parquet; cloudFiles.format/schemaLocation removed. Schema is supplied to readStream explicitly"
                          + (" (POC: inferred at runtime from existing source Parquet files)."
                             if rule == "X1-READER" and not schemas.get(statement.targets[0].id) else " (configured DDL).")
                          + " Source glob preserved. Schema evolution parity requires review; initial load, new files, restart and deduplication require runtime testing. No ADB state is copied."),
            "X1-WAIT": ("Await existing availableNow sink",
                        "Existing Delta sink/checkpoint retained and query awaited; no duplicate sink created. Durable progress is created by Spark at runtime. Use a fresh checkpoint with a planned initial load/deduplication."),
            "X1-RESET": ("Removed unconditional checkpoint/schema reset",
                         "Removed this exact matched streaming checkpoint/schema path deletion from converted code; progress is retained. Deliberate replay requires a separate controlled reset procedure."),
            "X1-RESET-LOG": ("Updated checkpoint status message",
                             "Replaced stale checkpoint-cleared output with an explicit preservation message."),
        }[rule]
        items.append(Item(file, cell.index, cell.start_line + start, rule, level, title,
                          original, converted[0],
                          message))
    ctx.need("autoloader", "Validate configured schema, source glob discovery, new files, restart, duplicate/replay behavior, schema additions and corrupt files. Start with a new checkpoint; do not copy ADB state.")
    return items, {}
