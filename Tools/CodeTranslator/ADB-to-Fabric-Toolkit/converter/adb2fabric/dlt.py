from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .config import CONFIG
from .model import AUTO, REVIEW, Cell, Item

IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
QUALIFIED = rf"{IDENTIFIER}(?:\.{IDENTIFIER})*"
SQL_TOKENS = re.compile(r"'(?:''|[^'])*'|" + QUALIFIED + r"|\s+|.", re.S)


class UnsupportedDLT(ValueError):
    pass


@dataclass
class Dataset:
    name: str
    kind: str
    query: str
    checks: dict[str, str]
    cell: Cell
    node: ast.FunctionDef
    decorators: list[tuple[ast.expr, str]]


def _literal(node, constants):
    if isinstance(node, ast.Name):
        if node.id not in constants:
            raise UnsupportedDLT(f"Expectation dictionary '{node.id}' is not a preceding static literal.")
        return constants[node.id]
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise UnsupportedDLT("DLT arguments must be static literals.") from exc


def _query(sql):
    if not isinstance(sql, str):
        raise UnsupportedDLT("spark.sql query must be a literal string.")
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1]
    tokens = SQL_TOKENS.findall(sql)
    if not tokens or not re.match(r"^SELECT\b", sql, re.I):
        raise UnsupportedDLT("Only literal SELECT queries are supported by the batch SQL converter.")
    if any(token in (";", "`", '"', "'") or token.startswith(("--", "/*")) for token in tokens):
        raise UnsupportedDLT("Multiple statements, quoted identifiers and SQL comments need manual SQL analysis.")
    if re.search(r"--|/\*|\*/", "".join(t for t in tokens if not t.startswith("'"))):
        raise UnsupportedDLT("SQL comments need manual SQL analysis.")
    if any(t.upper() in {"WITH", "STREAM", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE",
                         "DROP", "ALTER", "LATERAL"} for t in tokens if not t.startswith("'")):
        raise UnsupportedDLT("Streaming, nested CTEs and non-SELECT SQL need manual SQL analysis.")
    return sql


def _references(sql, resolve):
    tokens = SQL_TOKENS.findall(sql)
    for index, token in enumerate(tokens):
        if token.upper() not in ("FROM", "JOIN"):
            continue
        following = index + 1
        while following < len(tokens) and tokens[following].isspace():
            following += 1
        if following >= len(tokens) or not re.fullmatch(QUALIFIED, tokens[following]):
            raise UnsupportedDLT("Subqueries and nonliteral FROM/JOIN relations need manual SQL analysis.")
        relation = tokens[following]
        tail = following + 1
        while tail < len(tokens) and tokens[tail].isspace():
            tail += 1
        if tail < len(tokens) and tokens[tail] == "(":
            raise UnsupportedDLT("Table-valued functions require manual SQL analysis.")
        # Comma-style FROM joins are deliberately not inferred.
        cursor = following + 1
        while cursor < len(tokens) and tokens[cursor].upper() not in {
            "JOIN", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "ON"
        }:
            if tokens[cursor] == ",":
                raise UnsupportedDLT("Comma-style FROM joins need manual SQL analysis.")
            cursor += 1
        tokens[following] = resolve(relation)
    return "".join(tokens)


def _function(cell, node, constants):
    if (node.args.posonlyargs or node.args.args or node.args.kwonlyargs or node.args.vararg
            or node.args.kwarg or node.returns or getattr(node, "type_params", [])
            or len(node.body) != 1 or not isinstance(node.body[0], ast.Return)):
        raise UnsupportedDLT("Batch datasets must be zero-argument functions returning spark.sql directly.")
    call = node.body[0].value
    if (not isinstance(call, ast.Call) or ast.unparse(call.func) != "spark.sql"
            or len(call.args) != 1 or call.keywords):
        raise UnsupportedDLT("Only a direct return spark.sql('literal SELECT ...') body is supported.")
    query = _query(_literal(call.args[0], {}))
    name, kind, checks, decorators = node.name, None, {}, []
    for decorator in node.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        dotted = ast.unparse(func)
        if dotted in ("dlt.table", "dlt.view"):
            if kind is not None:
                raise UnsupportedDLT("Multiple dataset decorators are not supported.")
            kind = dotted.rsplit(".", 1)[-1]
            if isinstance(decorator, ast.Call):
                if decorator.args or any(kw.arg != "name" for kw in decorator.keywords):
                    raise UnsupportedDLT("Only the static name= dataset decorator argument is supported.")
                for kw in decorator.keywords:
                    name = _literal(kw.value, {})
            decorators.append((decorator, "TABLE" if kind == "table" else "VIEW"))
        elif dotted == "dlt.expect_all_or_drop":
            if checks or not isinstance(decorator, ast.Call) or len(decorator.args) != 1 or decorator.keywords:
                raise UnsupportedDLT("Only one static expect_all_or_drop dictionary is supported.")
            checks = _literal(decorator.args[0], constants)
            if not isinstance(checks, dict) or not checks:
                raise UnsupportedDLT("Expectations must be a non-empty literal dictionary.")
            for key, expression in checks.items():
                if not isinstance(key, str) or not re.fullmatch(IDENTIFIER, key) or not isinstance(expression, str) or not expression.strip():
                    raise UnsupportedDLT("Expectation names must be simple SQL identifiers and conditions must be literal strings.")
                if re.search(r";|--|/\*|\*/|\b(?:SELECT|FROM|WITH|DROP|CREATE|INSERT|DELETE|UPDATE|MERGE)\b", expression, re.I):
                    raise UnsupportedDLT("Expectation condition requires manual SQL analysis.")
            decorators.append((decorator, "EXPECT"))
        else:
            raise UnsupportedDLT(f"Unsupported DLT/decorator API: {dotted}.")
    if kind is None or not isinstance(name, str) or not re.fullmatch(IDENTIFIER, name):
        raise UnsupportedDLT("Dataset names must be simple literal identifiers.")
    if kind == "view" and checks:
        raise UnsupportedDLT("Expectations on logical views require manual semantic analysis.")
    return Dataset(name, kind, query, checks, cell, node, decorators)


def _plan(cells, settings, standalone):
    if not standalone:
        raise UnsupportedDLT("This notebook is referenced as a shared helper; no MLV creation was inserted.")
    constants, datasets, imports = {}, {}, []
    for cell in cells:
        if cell.kind == "markdown":
            continue
        if cell.kind != "code":
            raise UnsupportedDLT("Mixed-language or magic executable cells require manual pipeline analysis.")
        try:
            tree = ast.parse("\n".join(cell.lines))
        except SyntaxError as exc:
            raise UnsupportedDLT("The Python source could not be parsed safely.") from exc
        previous_end = 0
        for node in tree.body:
            if node.lineno <= previous_end:
                raise UnsupportedDLT("Multiple top-level statements on one line require manual analysis.")
            previous_end = node.end_lineno
            if isinstance(node, ast.Import) and any(alias.name == "dlt" for alias in node.names):
                if len(node.names) != 1 or node.names[0].asname:
                    raise UnsupportedDLT("Aliased/combined DLT imports require manual analysis.")
                imports.append((cell, node))
            elif isinstance(node, ast.FunctionDef):
                dataset = _function(cell, node, constants)
                key = dataset.name.casefold()
                if key in datasets:
                    raise UnsupportedDLT("Duplicate case-insensitive DLT dataset names.")
                datasets[key] = dataset
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in constants or name in {"spark", "dlt"}:
                    raise UnsupportedDLT("Reassigned configuration or reserved names require manual analysis.")
                constants[name] = _literal(node.value, {})
            else:
                raise UnsupportedDLT("Only import dlt, static literal assignments and supported dataset functions can be converted as a batch unit.")
    if len(imports) != 1 or not datasets or not any(d.kind == "table" for d in datasets.values()):
        raise UnsupportedDLT("A complete batch unit needs one import dlt and at least one supported table.")
    targets = {key.casefold(): value for key, value in settings["target_mappings"].items()}
    sources = {key.casefold(): value for key, value in settings["source_mappings"].items()}
    physical_targets = {value.casefold(): key for key, value in targets.items()}
    if any(dataset.node.name in constants for dataset in datasets.values()):
        raise UnsupportedDLT("Dataset functions conflict with configuration variable names.")
    for key, dataset in datasets.items():
        if dataset.kind == "table" and key not in targets:
            raise UnsupportedDLT(f"Configure dlt.target_mappings['{dataset.name}'] with an explicit Fabric target.")
        if key in sources or "live." + key in sources:
            raise UnsupportedDLT("Source mappings must not override local DLT dataset dependencies.")
    queries, dependencies, visiting, order = {}, {}, set(), []

    def visit(key):
        if key in visiting:
            raise UnsupportedDLT("Cyclic DLT dataset dependencies require manual redesign.")
        if key in queries:
            return
        visiting.add(key)
        dependencies[key] = []

        def resolve(relation):
            rel = relation.casefold()
            mapped = sources.get(rel, relation)
            local = physical_targets.get(mapped.casefold(), rel[5:] if rel.startswith("live.") else rel)
            if re.fullmatch(r"_a2f_view_\d+", rel):
                raise UnsupportedDLT("Source relation conflicts with reserved generated CTE names.")
            if local in datasets:
                visit(local)
                dependencies[key].append(local)
                ds = datasets[local]
                return f"_a2f_view_{list(datasets).index(local)}" if ds.kind == "view" else targets[local]
            if rel in sources:
                return mapped
            if rel.startswith("live."):
                raise UnsupportedDLT(f"Unresolved LIVE relation '{relation}'; configure dlt.source_mappings.")
            return relation

        queries[key] = _references(datasets[key].query, resolve)
        visiting.remove(key)
        order.append(key)

    for key in datasets:
        visit(key)
    statements = {}
    for key in order:
        dataset = datasets[key]
        if dataset.kind != "table":
            continue
        needed = set()

        def collect_views(parent):
            for dep in dependencies[parent]:
                if datasets[dep].kind == "view":
                    needed.add(dep)
                    collect_views(dep)

        collect_views(key)
        ctes = [f"_a2f_view_{list(datasets).index(dep)} AS (\n{queries[dep]}\n)"
                for dep in order if dep in needed]
        query = ("WITH " + ",\n".join(ctes) + "\n" if ctes else "") + queries[key]
        checks = [f"CONSTRAINT {name} CHECK ({condition}) ON MISMATCH DROP"
                  for name, condition in dataset.checks.items()]
        constraints = "\n(\n  " + ",\n  ".join(checks) + "\n)" if checks else ""
        create = "CREATE OR REPLACE" if settings["replace_existing"] else "CREATE"
        statements[key] = f"{create} MATERIALIZED LAKE VIEW {targets[key]}{constraints}\nAS\n{query}"
    return imports, datasets, statements


def convert_dlt(cells, source, ctx, standalone):
    settings = CONFIG.get("dlt", {})
    if not settings.get("enabled", False):
        return [], [], ""
    text = "\n".join("\n".join(c.lines) for c in cells if c.kind != "markdown")
    if not re.search(r"\b(?:import\s+dlt|from\s+dlt|dlt\.)", text):
        return [], [], ""
    if re.search(r"\b(?:dlt\.apply_changes|dlt\.create_auto_cdc_flow|stored_as_scd_type)\b", text):
        reason = "Streaming CDC/SCD2 detected: preserve keys, sequence ordering, deletes and history with an explicit streaming/Delta merge design; batch MLV conversion is not equivalent."
    elif re.search(r"\breadStream\b|\bdlt\.(?:read_stream|create_streaming_table)\b", text):
        reason = "Streaming DLT detected: preserve streaming source, checkpoints and sink semantics; do not convert this pipeline to batch MLV."
    else:
        try:
            imports, datasets, statements = _plan(cells, settings, standalone)
        except UnsupportedDLT as exc:
            reason = str(exc)
        else:
            items = []
            for cell, node in imports:
                original = "\n".join(cell.lines[node.lineno - 1:node.end_lineno])
                cell.lines[node.lineno - 1:node.end_lineno] = ["pass  # [ADB2FABRIC] DLT import removed; batch SQL MLV definitions generated"] + [""] * (node.end_lineno - node.lineno)
                items.append(Item(source, cell.index, cell.start_line + node.lineno - 1, "X3-IMPORT", AUTO,
                                  "DLT import removed", original, cell.lines[node.lineno - 1],
                                  "All DLT references in this supported batch unit were migrated; no fmlv preview dependency is required."))
            for key, dataset in datasets.items():
                cell, node = dataset.cell, dataset.node
                start = min(dec.lineno for dec in node.decorator_list)
                for decorator, category in dataset.decorators:
                    sql = statements.get(key, "Logical view inlined as a CTE in dependent MLV definitions.")
                    auto = category == "EXPECT"
                    items.append(Item(source, cell.index, cell.start_line + decorator.lineno - 1,
                                      "X3-" + category, AUTO if auto else REVIEW,
                                      {"TABLE": "Batch DLT table -> Spark SQL MLV",
                                       "VIEW": "DLT logical view -> persistent-query CTE",
                                       "EXPECT": "Static expectations -> ON MISMATCH DROP constraints"}[category],
                                      ast.unparse(decorator), sql,
                                      "SQL definition generated. Validate constraints, target names, dependency lineage and Fabric MLV refresh scheduling; no pipeline schedule was generated."))
                cell.lines[start - 1:node.end_lineno] = [
                    "pass  # [ADB2FABRIC] DLT dataset migrated to batch MLV definitions"
                ] + [""] * (node.end_lineno - start)
            generated = [(-3, "code", ["# REVIEW[ADB2FABRIC] Batch MLV definitions; verify lakehouse, constraints and refresh scheduling."]
                          + [f"spark.sql({statement!r})" for statement in statements.values()])]
            ctx.need("dlt", "Batch SQL MLV definitions generated. Configure dependency/refresh scheduling and validate outputs; OR REPLACE is " + str(settings["replace_existing"]) + ".")
            return items, generated, ""
    ctx.need("dlt", reason)
    return [], [], reason


def describe_dlt(line, reason):
    if "apply_changes" in line or "create_auto_cdc_flow" in line or "stored_as_scd_type" in line:
        category, title = "SCD2", "DLT CDC / SCD2 orchestration"
    elif "create_streaming_table" in line or "read_stream" in line:
        category, title = "STREAM", "DLT streaming API"
    elif re.search(r"\b(?:import\s+dlt|from\s+dlt)", line):
        category, title = "IMPORT", "DLT import (dependent APIs remain)"
    elif "expect" in line:
        category, title = "EXPECT", "DLT quality expectations"
    elif "dlt.view" in line:
        category, title = "VIEW", "DLT logical view"
    elif "dlt.table" in line:
        category, title = "TABLE", "DLT dataset declaration"
    else:
        category, title = "API", "DLT API / pipeline dependency"
    return "X3-" + category, title, reason + " Original code retained; manual migration required."
