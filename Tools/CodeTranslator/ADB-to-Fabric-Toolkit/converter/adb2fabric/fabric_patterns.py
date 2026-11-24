"""Guarded Fabric mappings for orchestration, catalog names, MLflow and temporary paths."""
from __future__ import annotations

import ast
import re

from .config import CONFIG
from .model import AUTO, MANUAL, REVIEW, Item
from .paths import fs_path_arguments


IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
THREE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2}\Z")
SQL_TOKEN = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|`(?:``|[^`])*`|"
    r'"(?:""|[^"])*"|[A-Za-z_][A-Za-z0-9_]*|\s+|.',
    re.S,
)
PY_STRING = re.compile(r"(?is)^([ru]*)(\"\"\"|'''|\"|')")


def mlflow_imports(cells):
    imported, assigned = {}, set()
    for cell in cells:
        if cell.kind != "code":
            continue
        try:
            tree = ast.parse("\n".join(cell.lines))
        except SyntaxError:
            continue
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name == "mlflow":
                        imported.setdefault(alias.asname or "mlflow",
                                            (cell.index, statement.lineno, statement.col_offset))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned.add(node.id)
            elif isinstance(node, ast.arg):
                assigned.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assigned.add(node.name)
    return {name: position for name, position in imported.items() if name not in assigned}


def _parts(receiver):
    while isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute):
        receiver = receiver.func.value
    return receiver


def _table_argument(call):
    if not isinstance(call.func, ast.Attribute):
        return None, False
    method, receiver = call.func.attr, call.func.value
    base = _parts(receiver)
    recognized, index, keywords = False, 0, {"tableName", "name"}
    if method == "table":
        recognized = (isinstance(receiver, ast.Name) and receiver.id == "spark") or (
            isinstance(base, ast.Attribute) and base.attr in ("read", "readStream")
            and isinstance(base.value, ast.Name) and base.value.id == "spark"
        )
    elif method in ("saveAsTable", "toTable"):
        recognized = isinstance(base, ast.Attribute) and base.attr in ("write", "writeStream")
    elif method == "forName" and isinstance(receiver, ast.Name) and receiver.id == "DeltaTable":
        recognized, index, keywords = True, 1, {"tableOrViewName"}
    else:
        return None, False
    named = [kw.value for kw in call.keywords if kw.arg in keywords]
    if len(named) > 1 or (len(call.args) > index and named):
        recognized = False
    argument = call.args[index] if len(call.args) > index else (named[0] if named else None)
    return argument, recognized


def _table_target(name):
    mapping = CONFIG.get("table_mappings", {})
    return next((target for source, target in mapping.items() if source.casefold() == name.casefold()), None)


def _table_item(file, cell, line, original, name, converted, ctx, unsupported=""):
    target = _table_target(name)
    identity = target is not None and target.casefold() == name.casefold()
    if unsupported or not identity:
        ctx.need("unity", f"{name}: confirm the Fabric lakehouse/schema, table contents and permissions.")
    if unsupported:
        return Item(file, cell, line, "T-UC", MANUAL, "Unity Catalog table reference",
                    original, original, unsupported)
    if target is None:
        return Item(file, cell, line, "T-UC", REVIEW, "Three-part table reference",
                    original, original,
                    f"No table_mappings entry for {name}. It may be a Databricks catalog reference; "
                    "confirm the Fabric identifier or configure an exact mapping.")
    if identity:
        ctx.need("lakehouse", f"Retained {name} as configured; verify the Fabric table and permissions.")
        return Item(file, cell, line, "T-UC", AUTO, "Configured unchanged table identifier",
                    original, converted,
                    f"Retained {name} using the configured identity mapping. Verify target data, permissions and write mode in Fabric.")
    ctx.need("lakehouse", f"Mapped {name} to {target}; confirm the default lakehouse or explicit target.")
    return Item(file, cell, line, "T-UC", AUTO, "Configured Unity Catalog table mapping",
                original, converted,
                f"Mapped {name} to {target}. Verify target data, schema, permissions and write mode in Fabric.")


def _sql_ident(token):
    if IDENT.fullmatch(token):
        return token
    if token.startswith("`") and token.endswith("`") and IDENT.fullmatch(token[1:-1]):
        return token[1:-1]
    return None


def _sql_refs(text):
    tokens = [m for m in SQL_TOKEN.finditer(text)
              if not (m.group().isspace() or m.group().startswith(("--", "/*")))]
    depth, from_depths, merge_depths = 0, set(), set()
    from_end = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "QUALIFY", "WINDOW",
                "UNION", "INTERSECT", "EXCEPT"}
    for index, token in enumerate(tokens):
        word = token.group().upper()
        if word == "(":
            depth += 1
            continue
        if word == ")":
            from_depths.discard(depth)
            merge_depths.discard(depth)
            depth = max(0, depth - 1)
            continue
        if word == ";":
            from_depths.clear()
            merge_depths.clear()
            continue
        if word in from_end:
            from_depths.discard(depth)
        if word == "MERGE" and index + 1 < len(tokens) and tokens[index + 1].group().upper() == "INTO":
            merge_depths.add(depth)
        if word == "WHEN":
            merge_depths.discard(depth)
        if word == "FROM":
            from_depths.add(depth)
        relation = (word in ("FROM", "JOIN", "INTO", "UPDATE", "TABLE")
                    or (word == "," and depth in from_depths)
                    or (word == "USING" and depth in merge_depths))
        if not relation:
            continue
        if word == "TABLE" and (index == 0 or tokens[index - 1].group().upper() not in
                                ("CREATE", "REPLACE", "ALTER", "DROP", "TRUNCATE", "OVERWRITE", "DESCRIBE", "REFRESH")):
            continue
        start = index + 1
        while start < len(tokens) and tokens[start].group().upper() in ("IF", "NOT", "EXISTS", "ONLY"):
            start += 1
        if start + 4 >= len(tokens):
            continue
        selected = tokens[start:start + 5]
        parts = [_sql_ident(selected[i].group()) for i in (0, 2, 4)]
        if not (all(parts) and selected[1].group() == "." and selected[3].group() == "."):
            continue
        if start + 5 < len(tokens) and tokens[start + 5].group() in (".", "("):
            continue
        a, b = selected[0].start(), selected[-1].end()
        raw = text[a:b]
        if "\n" in raw or "\r" in raw or "--" in raw or "/*" in raw:
            yield a, b, ".".join(parts), raw, True, False
        else:
            quoted = any(selected[i].group().startswith("`") for i in (0, 2, 4))
            yield a, b, ".".join(parts), raw, False, quoted


def _sql_findings(text, ctx, file, cell, line_at, allow_edits=True):
    findings, edits = [], []
    for start, end, name, raw, awkward, quoted in _sql_refs(text):
        target = _table_target(name)
        if target is None and name.casefold().startswith("hive_metastore.") and "`" not in raw:
            continue  # The existing X4 rule reports this legacy catalog.
        new = ".".join(f"`{part}`" for part in target.split(".")) if target and quoted else target
        unsupported = ""
        if target and (awkward or not allow_edits):
            unsupported = "SQL identifier spans lines/comments or an escaped Python literal; apply the configured mapping manually."
        item = _table_item(file, cell, line_at(start), raw, name, new or raw, ctx, unsupported)
        findings.append((start, item))
        if target and not unsupported:
            edits.append((start, end, new))
    return findings, edits


def convert_sql_patterns(lines, ctx, file, cell, start_line):
    text = "\n".join(lines)
    findings, edits = _sql_findings(
        text, ctx, file, cell, lambda offset: start_line + text.count("\n", 0, offset)
    )
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    evidence = {}
    for _, item in findings:
        evidence.setdefault(item.line - start_line, []).append(item)
    return text.split("\n"), evidence


def convert_code_patterns(lines, ctx, file, cell, start_line, aliases, convert_runs, convert_moves):
    text = "\n".join(lines)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return lines, {}, False
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1

    def pos(line, column):
        return offsets[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))

    def span(node):
        return pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)

    def segment(node):
        a, b = span(node)
        return text[a:b]

    evidence, edits = {}, []

    def record(node, item, changes=()):
        evidence.setdefault(node.lineno - 1, []).append(item)
        edits.extend(changes)

    def notebook_run(node):
        arguments, known = {}, True
        names = ("path", "timeout_seconds", "arguments")
        if len(node.args) > len(names) or any(isinstance(arg, ast.Starred) for arg in node.args):
            known = False
        for name, value in zip(names, node.args):
            arguments[name] = value
        for kw in node.keywords:
            name = {"timeout": "timeout_seconds", "timeoutSeconds": "timeout_seconds"}.get(kw.arg, kw.arg)
            if name not in names or name in arguments:
                known = False
            else:
                arguments[name] = kw.value
        original = segment(node)
        if not known or "path" not in arguments:
            message = "Unsupported notebook.run arguments; retain the Databricks call and migrate it manually."
            ctx.need("run", message)
            record(node, Item(file, cell, start_line + node.lineno - 1, "N1", MANUAL,
                              "dbutils.notebook.run", original, original, message))
            return
        changes = [(*span(node.func), "notebookutils.notebook.run")]
        target = arguments["path"]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            mapped = CONFIG.get("notebook_mappings", {}).get(target.value)
            if mapped:
                changes.append((*span(target), repr(mapped)))
                detail = f"{target.value!r} -> {mapped!r}; verify the Fabric child notebook."
            else:
                detail = f"{target.value!r}: configure notebook_mappings if this path is not a Fabric notebook name."
        else:
            detail = "Dynamic notebook target: ensure its runtime value is a Fabric notebook name."
        for kw in node.keywords:
            if kw.arg in ("timeout", "timeoutSeconds"):
                begin = pos(kw.lineno, kw.col_offset)
                changes.append((begin, begin + len(kw.arg), "timeout_seconds"))
        start, end = span(node)
        converted = text[start:end]
        for a, b, replacement in sorted(changes, reverse=True):
            converted = converted[:a - start] + replacement + converted[b - start:]
        message = ("Converted to Fabric notebookutils.notebook.run; verify target notebook, arguments, timeout, "
                   "workspace and child/parent lakehouse compatibility. " + detail)
        ctx.need("run", detail)
        record(node, Item(file, cell, start_line + node.lineno - 1, "N1", REVIEW,
                          "dbutils.notebook.run -> notebookutils.notebook.run", original, converted, message), changes)

    call_functions = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    if convert_moves:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and id(node) not in call_functions
                    and ast.unparse(node) == "dbutils.fs.mv"):
                original = segment(node)
                message = "A stored move-method reference may receive Databricks recurse arguments; migrate its uses manually."
                ctx.need("storage", message)
                record(node, Item(file, cell, start_line + node.lineno - 1, "F-MV", MANUAL,
                                  "Databricks filesystem move reference", original, original, message))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        function = ast.unparse(node.func)
        if convert_runs and function == "dbutils.notebook.run":
            notebook_run(node)
        if convert_moves and function == "dbutils.fs.mv":
            original = segment(node)
            if len(node.args) == 2 and not node.keywords and not any(
                    isinstance(arg, ast.Starred) for arg in node.args):
                begin, end = span(node.func)
                replacement = "notebookutils.fs.mv"
                converted = original[:begin - span(node)[0]] + replacement + original[end - span(node)[0]:]
                message = ("Moved the basic two-argument call to Fabric; verify directory recursion, parent creation "
                           "and overwrite behavior. Fabric's third argument is create_path, not Databricks recurse.")
                item = Item(file, cell, start_line + node.lineno - 1, "F-MV", REVIEW,
                            "dbutils.fs.mv -> notebookutils.fs.mv", original, converted, message)
                record(node, item, [(begin, end, replacement)])
            else:
                message = ("Databricks mv recurse and Fabric mv create_path/overwrite have different semantics; "
                           "migrate this call manually without reusing the third argument.")
                item = Item(file, cell, start_line + node.lineno - 1, "F-MV", MANUAL,
                            "Databricks filesystem move", original, original, message)
                record(node, item)
            ctx.need("storage", message)

        arg, recognized = _table_argument(node)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and THREE_PART.fullmatch(arg.value):
            if arg.value.casefold().startswith("hive_metastore.") and not _table_target(arg.value):
                continue
            original = segment(arg)
            target = _table_target(arg.value)
            unsupported = "" if recognized else "Ambiguous Spark table call or unverified writer alias; map this table reference manually."
            converted = repr(target) if target else original
            item = _table_item(file, cell, start_line + arg.lineno - 1, original, arg.value, converted, ctx, unsupported)
            record(arg, item, [(*span(arg), converted)] if target and not unsupported else [])
        elif arg is not None and (
                (recognized and not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)))
                or (isinstance(arg, ast.JoinedStr) and any(
                    isinstance(v, ast.Constant) and isinstance(v.value, str)
                    and re.search(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\.", v.value) for v in arg.values))):
            original = segment(arg)
            literals = [v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str)] \
                if isinstance(arg, ast.JoinedStr) else []
            simple_dynamic = (isinstance(arg, ast.JoinedStr) and any(isinstance(v, ast.FormattedValue) for v in arg.values)
                              and sum(value.count(".") for value in literals) <= 1
                              and all(re.fullmatch(r"[A-Za-z0-9_.]*", value) for value in literals))
            level = REVIEW if simple_dynamic else MANUAL
            message = ("Verify the runtime table identifier resolves to the intended Fabric lakehouse/schema."
                       if simple_dynamic else
                       "Dynamic table identifier cannot be inspected or mapped safely; supply an explicit Fabric identifier.")
            ctx.need("unity", message)
            record(arg, Item(file, cell, start_line + arg.lineno - 1, "T-UC", level,
                             "Dynamic table reference", original, original, message))

        sql_keywords = [kw.value for kw in node.keywords if kw.arg == "sqlQuery"]
        sql = (node.args[0] if node.args and not sql_keywords else
               sql_keywords[0] if not node.args and len(sql_keywords) == 1 else None)
        if function == "spark.sql" and isinstance(sql, ast.Constant) and isinstance(sql.value, str):
            literal = segment(sql)
            match = PY_STRING.match(literal)
            quote = match.group(2) if match else ""
            raw = literal[match.end():-len(quote)] if match and literal.endswith(quote) else None
            safe = raw == sql.value
            first = span(sql)[0] + match.end() if safe else 0
            findings, sql_edits = _sql_findings(
                sql.value, ctx, file, cell,
                lambda offset: start_line + sql.lineno - 1 + sql.value.count("\n", 0, offset),
                allow_edits=safe,
            )
            for _, item in findings:
                evidence.setdefault(item.line - start_line, []).append(item)
            if safe:
                edits.extend((first + a, first + b, replacement) for a, b, replacement in sql_edits)
        elif function == "spark.sql":
            original = segment(node)
            message = "Dynamic or unsupported spark.sql query cannot be inspected for Databricks table references; migrate it manually."
            ctx.need("unity", message)
            record(node, Item(file, cell, start_line + node.lineno - 1, "T-UC", MANUAL,
                              "Dynamic or unsupported Spark SQL query", original, original, message))

        for path in fs_path_arguments(node):
            prefix = path.value if isinstance(path, ast.Constant) and isinstance(path.value, str) else (
                path.values[0].value if isinstance(path, ast.JoinedStr) and path.values
                and isinstance(path.values[0], ast.Constant) and isinstance(path.values[0].value, str) else "")
            if prefix != "/tmp" and not prefix.startswith("/tmp/"):
                continue
            mapped = any(m.get("enabled", True) and m.get("scope") == "dbutils_fs"
                         and (prefix == m["source"].rstrip("/") or prefix.startswith(m["source"].rstrip("/") + "/"))
                         for m in CONFIG.get("path_mappings", []))
            if not mapped:
                original = segment(path)
                message = "DBFS /tmp path is not a Fabric Lakehouse path. Add an explicit dbutils_fs-scoped path mapping."
                ctx.need("storage", message)
                record(path, Item(file, cell, start_line + path.lineno - 1, "P-TMP", MANUAL,
                                  "Unmapped Databricks temporary filesystem path", original, original, message))

        if not isinstance(node.func.value, ast.Name):
            continue
        alias, method = node.func.value.id, node.func.attr
        if alias not in aliases and alias != "mlflow":
            continue
        imported = alias in aliases and aliases[alias] < (cell, node.lineno, node.col_offset)
        config = CONFIG.get("mlflow", {})
        original = segment(node)
        if method in ("set_tracking_uri", "set_registry_uri"):
            uri_arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "uri"), None)
            if uri_arg is None or not isinstance(uri_arg, ast.Constant):
                message = "Dynamic or unsupported MLflow tracking/registry URI cannot be verified; migrate it manually."
                ctx.need("mlflow", message)
                record(node, Item(file, cell, start_line + node.lineno - 1, "ML1", MANUAL,
                                  "Dynamic MLflow URI", original, original, message))
                continue
            uri = uri_arg.value
            if not isinstance(uri, str) or not (uri == "databricks" or uri.startswith(("databricks://", "databricks-uc"))):
                continue
            statement = next((s for s in tree.body if isinstance(s, ast.Expr) and s.value is node), None)
            simple_args = (len(node.args) == 1 and not node.keywords) or (
                not node.args and len(node.keywords) == 1 and node.keywords[0].arg == "uri")
            supported = (uri == "databricks" and method == "set_tracking_uri" and statement is not None and imported
                         and simple_args and "#" not in segment(statement)
                         and config.get("use_fabric_default_tracking", False))
            if supported:
                a, b = span(statement)
                replacement = "pass" + "\n" * (statement.end_lineno - statement.lineno)
                message = "Removed Databricks tracking URI; validate Fabric's effective default URI, workspace and permissions before logging."
                item = Item(file, cell, start_line + statement.lineno - 1, "ML1", REVIEW,
                            "MLflow Databricks tracking -> Fabric default", segment(statement), replacement, message)
                record(statement, item, [(a, b, replacement)])
            else:
                message = "Databricks MLflow tracking/registry URI or call form requires explicit Fabric migration."
                item = Item(file, cell, start_line + node.lineno - 1, "ML1", MANUAL,
                            "Databricks MLflow URI", original, original, message)
                record(node, item)
            ctx.need("mlflow", message)
        elif method == "set_experiment":
            arg = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "experiment_name"), None)
            keywords = [kw.arg for kw in node.keywords]
            valid = (len(node.args) <= 1 and not any(isinstance(a, ast.Starred) for a in node.args)
                     and all(k in ("experiment_name", "experiment_id") for k in keywords)
                     and len(keywords) == len(set(keywords))
                     and not (node.args and "experiment_name" in keywords)
                     and not (arg is not None and "experiment_id" in keywords))
            if not valid:
                message = "Unsupported MLflow experiment arguments require manual migration."
                ctx.need("mlflow", message)
                record(node, Item(file, cell, start_line + node.lineno - 1, "ML2", MANUAL,
                                  "Unsupported MLflow experiment call", original, original, message))
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/"):
                target = config.get("experiment_mappings", {}).get(arg.value)
                if target and imported:
                    replacement = repr(target)
                    message = "Mapped the Databricks experiment path to a configured Fabric experiment name; verify lineage and permissions."
                    item = Item(file, cell, start_line + arg.lineno - 1, "ML2", REVIEW,
                                "Databricks experiment -> Fabric experiment", segment(arg), replacement, message)
                    record(arg, item, [(*span(arg), replacement)])
                else:
                    message = "Configure an exact mlflow.experiment_mappings entry and ensure the mlflow module is imported."
                    item = Item(file, cell, start_line + arg.lineno - 1, "ML2", MANUAL,
                                "Unmapped Databricks experiment path", segment(arg), segment(arg), message)
                    record(arg, item)
                ctx.need("mlflow", message)
            elif arg is not None and not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                message = "Verify the dynamic experiment name resolves to a Fabric experiment, not a Databricks path."
                ctx.need("mlflow", message)
                record(arg, Item(file, cell, start_line + arg.lineno - 1, "ML2", REVIEW,
                                 "Dynamic MLflow experiment name", segment(arg), segment(arg), message))
            elif "experiment_id" in keywords:
                message = "Databricks experiment IDs are not portable; configure and verify the Fabric experiment ID."
                ctx.need("mlflow", message)
                record(node, Item(file, cell, start_line + node.lineno - 1, "ML2", REVIEW,
                                  "MLflow experiment ID", original, original, message))

    for begin, end, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    return text.split("\n"), evidence, True
