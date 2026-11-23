from __future__ import annotations

import ast
import hashlib
import io
import json
import posixpath
import re
import shlex
import tokenize
from pathlib import Path

from . import __version__
from .config import CONFIG
from .model import AUTO, MANUAL, REVIEW, TRIVIAL_RULES, Cell, Context, Item, NotebookResult
from .parser import parse_export
from .paths import map_paths
from .rules import HANDLERS, MAGIC_MESSAGES, RULES, convert_secrets, secret_conversion
from .task_values import convert_task_values, helper_references
from .dlt import convert_dlt, describe_dlt
from .autoloader import convert_autoloader
from .fabric_patterns import convert_code_patterns, convert_sql_patterns, mlflow_imports

TRIPLE = re.compile(r"\"\"\"|'''")
CELL_META = CONFIG["cell_metadata"]
CHECKLIST_TEXT = CONFIG["checklist_text"]


def _comment(lang: str) -> str:
    return "--" if lang == "sql" else "#"


def _process_lines(lines, kind, ctx, file, cell_idx, start_line, cmt=None, dlt_reason="", stream_reason="",
                   mlflow_aliases=None):
    """Apply the rule catalogue line by line. Returns (new_lines, items, manual_line_count, code_line_count)."""
    items: list[Item] = []
    out: list[str] = []
    deferred: list[str] = []
    manual_lines = code_lines = 0
    in_str = None
    sql_like = kind == "sql"
    cmt = cmt or ("--" if sql_like else "#")
    secret_rule = next((r for r in RULES if r.id == "S1" and r.sub is HANDLERS["secret"]
                        and not r.silent and r.level != MANUAL and r.scope in ("py", "any")), None)
    secret_lines, secret_evidence, secrets_parsed = (
        convert_secrets(lines, ctx, file, cell_idx, start_line) if kind == "code" and secret_rule else (lines, {}, False)
    )
    run_rule = next((r for r in RULES if r.id == "N1" and r.pattern == r"\bdbutils\.notebook\.run\("
                     and r.sub == "notebookutils.notebook.run(" and r.level == REVIEW
                     and not r.silent and r.scope in ("py", "any")), None)
    move_rule = next((r for r in RULES if r.id == "F1"
                      and r.pattern == r"\bdbutils\.fs\.(ls|cp|mv|rm|mkdirs|head|put|append)\b"
                      and r.sub == r"notebookutils.fs.\1" and r.level == AUTO
                      and not r.silent and r.scope in ("py", "any")), None)
    if kind == "code":
        pattern_lines, pattern_evidence, patterns_parsed = convert_code_patterns(
            secret_lines, ctx, file, cell_idx, start_line, mlflow_aliases or {}, bool(run_rule), bool(move_rule)
        )
    elif kind == "sql":
        pattern_lines, pattern_evidence = convert_sql_patterns(secret_lines, ctx, file, cell_idx, start_line)
        patterns_parsed = False
    else:
        pattern_lines, pattern_evidence, patterns_parsed = secret_lines, {}, False
    if kind == "code" and not patterns_parsed:
        first = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), None)
        if first is not None:
            original = lines[first].strip()
            message = ("The cell is not parseable as Python, so guarded migration patterns could not be analyzed. "
                       "Review notebook magics or syntax and migrate the cell manually.")
            pattern_evidence.setdefault(first, []).append(
                Item(file, cell_idx, start_line + first, "P-CELL", MANUAL,
                     "Unanalyzed Python cell", original, original, message))
    mapped_lines, path_evidence = map_paths(pattern_lines, kind)
    dbutils_lines = set()
    if patterns_parsed:
        dbutils_lines = {token.start[0] - 1 for token in tokenize.generate_tokens(io.StringIO("\n".join(pattern_lines)).readline)
                         if token.type == tokenize.NAME and token.string == "dbutils"}
    for off, line in enumerate(lines):
        starts_in_string = in_str is not None
        for tok in TRIPLE.findall(line):
            in_str = tok if in_str is None else (None if tok == in_str else in_str)
        stripped = line.strip()
        if not stripped or (stripped.startswith(cmt) and not starts_in_string):
            out.append(line)
            continue
        code_lines += 1
        cur = mapped_lines[off]
        line_items = list(secret_evidence.get(off, [])) + list(pattern_evidence.get(off, []))
        for mapping in path_evidence.get(off, []):
            detail = f"{mapping['id']}: {mapping['source']} -> {mapping['target']}"
            ctx.need("storage", f"Configured path mapping {detail}; verify storage access and destination before running, especially delete operations.")
            if mapping["target"].startswith(("Files/", "Tables/", "/lakehouse/default/Files/", "/lakehouse/default/Tables/")):
                ctx.need("lakehouse", f"Configured path mapping {mapping['id']} requires an attached default lakehouse.")
            line_items.append(Item(file, cell_idx, start_line + off, "P-MAP", AUTO,
                                   "Configured ADB -> Fabric path", stripped, cur.strip(),
                                   detail + "; automatically converted using the enabled mapping. Verify storage setup before execution."))
        for rule in RULES:
            if rule.id == "S1" and secrets_parsed:
                continue
            if rule.id == "N1" and kind == "code" and run_rule:
                continue
            if rule.id == "F1" and kind == "code" and move_rule and not patterns_parsed:
                continue
            if rule.id == "F1" and any(i.rule_id == "F-MV" and i.level == MANUAL for i in line_items):
                continue
            if rule.scope == "py" and kind != "code":
                continue
            if rule.scope == "sql" and kind != "sql":
                continue
            if rule.id == "Z9" and (any(i.level == MANUAL for i in line_items) or "dbutils" not in cur):
                continue
            if rule.id == "Z9" and patterns_parsed and off not in dbutils_lines:
                continue
            if not rule.rx.search(cur):
                continue
            if rule.checklist:
                ctx.need(*rule.checklist)
            if rule.silent:
                continue
            converted = cur
            if rule.level != MANUAL and rule.sub is not None and kind == "code":
                converted = rule.rx.sub((lambda mm, r=rule: r.sub(mm, ctx)) if callable(rule.sub) else rule.sub, cur)
            it = Item(file, cell_idx, start_line + off, rule.id, rule.level, rule.title, stripped, converted.strip(), rule.message)
            if rule.id == "S1":
                outcomes = [secret_conversion(m.group(0), ctx) for m in rule.rx.finditer(cur)]
                it.level = MANUAL if any(level == MANUAL for _, level, _ in outcomes) else (
                    REVIEW if any(level == REVIEW for _, level, _ in outcomes) else AUTO)
                it.message = " ".join(dict.fromkeys(message for _, _, message in outcomes))
            if rule.id == "W2" and all(m.group("name") in CONFIG.get("parameter_defaults", {})
                                       and m.group("name").isidentifier()
                                       for m in rule.rx.finditer(cur)):
                it.level = AUTO
                it.message = "Parameter reference and configured string default generated. Verify Fabric pipeline bindings."
            if rule.id == "X1" and stream_reason:
                it.message = stream_reason
            if rule.id == "X3" and dlt_reason:
                it.rule_id, it.title, it.message = describe_dlt(stripped, dlt_reason)
            line_items.append(it)
            cur = converted
        for item in line_items:
            if item.rule_id == "P-MAP":
                item.converted = cur.strip()
            elif item.rule_id == "F-MV":
                item.converted = "\n".join(map_paths(item.converted.split("\n"), "code")[0])
        items.extend(line_items)
        has_manual = any(i.level == MANUAL for i in line_items)
        if has_manual:
            manual_lines += 1
        selfref = re.match(r"^(\s*)(\w+)\s*=\s*\2\s*$", cur)
        if selfref:  # `x = x` left behind by widget conversion
            cur = f"{selfref.group(1)}# {selfref.group(2)} is provided by the parameters cell"
        tag = ""
        if line_items and "[ADB2FABRIC]" not in cur:
            worst = MANUAL if has_manual else (REVIEW if any(i.level == REVIEW for i in line_items) else AUTO)
            ids = ",".join(i.rule_id for i in line_items if i.level == worst)
            if worst == MANUAL:
                tag = f"TODO[ADB2FABRIC] {ids}: {line_items[[i.level for i in line_items].index(MANUAL)].title}"
            elif worst == REVIEW:
                tag = f"REVIEW[ADB2FABRIC] {ids}"
        if tag:
            safe_inline = (not starts_in_string and in_str is None and not TRIPLE.search(line)
                           and not cur.rstrip().endswith("\\") and not cur.lstrip().startswith(("%", "!")))
            if safe_inline or sql_like:
                cur = f"{cur.rstrip()}  {cmt} {tag}"
            else:
                deferred.append(f"{cmt} {tag} (source line {start_line + off}: inside a multi-line string / continuation)")
        out.append(cur)
    return deferred + out, items, manual_lines, code_lines


def _split_run(first: str):
    m = re.match(r"^%run\s+(.*)$", first.strip(), re.I)
    parts = shlex.split(m.group(1)) if m else []
    return (parts[0] if parts else ""), parts[1:]


def _fs_to_code(first: str):
    parts = shlex.split(re.sub(r"^%fs\s*", "", first.strip(), flags=re.I))
    if not parts:
        return None
    cmd, args = parts[0], parts[1:]
    recursive = "-r" in args
    args = [a for a in args if a != "-r"]
    mapping = CONFIG["fs_magic"]
    if cmd not in mapping["commands"] or (cmd == "mv" and recursive):
        return None
    call = f"{mapping['api_prefix']}.{cmd}(" + ", ".join(repr(a) for a in args) + (", True" if (cmd == "rm" and recursive) else "") + ")"
    return f"display({call})" if cmd in mapping["display_result_commands"] else call


def _syntax_check(cells_out) -> list[str]:
    errs = []
    for idx, (kind, lines) in cells_out:
        if kind != "code":
            continue
        probe = "\n".join("pass" if l.lstrip().startswith(("%", "!")) else l for l in lines)
        try:
            ast.parse(probe)
        except SyntaxError as e:
            errs.append(f"cell {idx}: {e.msg} (line {e.lineno})")
    return errs


def _nb_cell(kind, lines, cid, tags=None):
    src = [l + "\n" for l in lines[:-1]] + ([lines[-1]] if lines else [])
    if kind == "markdown":
        return {"cell_type": "markdown", "id": cid, "metadata": {}, "source": src}
    lang, group = CELL_META.get(kind, CELL_META["code"])
    meta = {"microsoft": {"language": lang, "language_group": group}}
    if tags:
        meta["tags"] = tags
    return {"cell_type": "code", "id": cid, "metadata": meta, "execution_count": None, "outputs": [], "source": src}


def convert_text(text: str, rel_source: str, language: str, standalone: bool = True):
    cells, warnings = parse_export(text, language)
    ctx = Context()
    header = text.lstrip("\ufeff").split("\n", 1)[0].strip()
    task_items, task_output = convert_task_values(
        cells, rel_source, ctx, standalone and header == "# Databricks notebook source" and language == "python"
    )
    dlt_items, dlt_generated, dlt_reason = convert_dlt(
        cells, rel_source, ctx, standalone and header == "# Databricks notebook source" and language == "python"
    )
    if dlt_reason:
        warnings.append("DLT migration not applied: " + dlt_reason)
    stream_items, stream_reasons = convert_autoloader(
        cells, rel_source, ctx, standalone and header == "# Databricks notebook source" and language == "python"
    )
    mlflow_aliases = mlflow_imports(cells)
    items: list[Item] = task_items + dlt_items + stream_items
    out: list[tuple[int, str, list[str]]] = []          # (source cell index, kind, lines)
    code_lines = manual_lines = 0
    if task_output:
        out.append((-2, "code", ["import json as _a2f_json", "fabric_task_values = {}"]))

    for c in cells:
        magic_item = None
        if c.kind in MAGIC_MESSAGES and c.magic:
            rid, lvl, title = MAGIC_MESSAGES[c.kind]
            magic_item = Item(rel_source, c.index, c.start_line, rid, lvl, title, c.magic, "", "")
        if c.kind == "markdown":
            out.append((c.index, "markdown", ([f"**{c.title}**", ""] if c.title else []) + c.lines))
        elif c.kind in ("code", "sql", "scala", "r"):
            kind = "code" if c.kind == "code" else c.kind
            if c.kind in ("scala", "r"):
                new, it, ml, cl = _process_lines(c.lines, "other", ctx, rel_source, c.index, c.start_line,
                                                 cmt="//" if c.kind == "scala" else "#", dlt_reason=dlt_reason)
            else:
                new, it, ml, cl = _process_lines(c.lines, kind, ctx, rel_source, c.index, c.start_line,
                                                 dlt_reason=dlt_reason, stream_reason=stream_reasons.get(c.index, ""),
                                                 mlflow_aliases=mlflow_aliases)
            items += it
            manual_lines += ml
            code_lines += cl
            head = CONFIG["cell_prefixes"].get(c.kind, [])
            title = [f"{'--' if c.kind == 'sql' else '#'} {c.title}"] if c.title else []
            out.append((c.index, kind, head + title + new))
        elif c.kind == "sh":
            new, it, ml, cl = _process_lines(c.lines, "other", ctx, rel_source, c.index, c.start_line)
            items += it
            manual_lines += ml
            code_lines += cl
            out.append((c.index, "code", [("!" + l.strip()) if l.strip() and not l.strip().startswith("#") else l for l in new]))
        elif c.kind == "run":
            target, extra = _split_run(c.lines[0])
            name = CONFIG.get("notebook_mappings", {}).get(target)
            if name is None:
                name = posixpath.basename(target.rstrip("/")) or target
                name = re.sub(r"\.(py|sql|ipynb)$", "", name)
            magic_name = name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else json.dumps(name, ensure_ascii=False)
            run_command = f"%run {magic_name}"
            out.append((c.index, "code", [run_command]))
            ctx.need("run", f"%run {target}: notebook '{name}' must exist in the same Fabric workspace")
            magic_item.converted = run_command
            if extra:
                case = CONFIG["manual_cases"]["run_with_parameters"]
                items.append(Item(rel_source, c.index, c.start_line, case["id"], case["level"], case["title"],
                                  c.lines[0], run_command, case["message"]))
            code_lines += 1
        elif c.kind == "fs":
            code = _fs_to_code(c.lines[0])
            if code is None:
                case = CONFIG["manual_cases"]["unsupported_fs"]
                magic_item = Item(rel_source, c.index, c.start_line, case["id"], case["level"], case["title"], c.lines[0], "", case["message"])
                out.append((c.index, "code", [f"# TODO[ADB2FABRIC] MAG-FS: unsupported %fs command", "# " + c.lines[0]]))
                manual_lines += 1
            else:
                new, it, ml, cl = _process_lines([code], "code", ctx, rel_source, c.index, c.start_line)
                items += it
                manual_lines += ml
                out.append((c.index, "code", new))
                magic_item.converted = "\n".join(new)
            code_lines += 1
        elif c.kind == "pip":
            out.append((c.index, "code", c.lines))
            ctx.need("library", "%pip install is session scoped in Fabric; use an Environment for production")
            code_lines += 1
        else:
            out.append((c.index, "code", ["# TODO[ADB2FABRIC] MAG-UNK: unsupported magic command; original below"] + ["# " + l for l in c.lines]))
            manual_lines += 1
            code_lines += 1
        if magic_item:
            if not magic_item.converted:
                magic_item.converted = f"{magic_item.title}"
            items.append(magic_item)
    if task_output:
        out.append((-1, "code", ["notebookutils.notebook.exit(_a2f_json.dumps(fabric_task_values, allow_nan=False))"]))
    out.extend(dlt_generated)
    return out, items, ctx, code_lines, manual_lines, warnings


def build_notebook(rel_source, out_cells, ctx, items, language, tool_version=__version__):
    nb_cells = []
    scored = [i for i in items if i.rule_id not in TRIVIAL_RULES]
    auto = sum(i.level == AUTO for i in scored)
    rev = sum(i.level == REVIEW for i in scored)
    man = sum(i.level == MANUAL for i in scored)
    summary = [f"### Converted from Azure Databricks by adb2fabric {tool_version}",
               f"Source: `{rel_source}`  |  Databricks-specific constructs: **{len(scored)}** (auto {auto}, review {rev}, manual {man})",
               "", "Search this notebook for `TODO[ADB2FABRIC]` (manual work) and `REVIEW[ADB2FABRIC]` (verify behaviour). "
               "Details are in the conversion report. This is a static conversion: it has not been executed."]
    if ctx.checklist:
        summary += ["", "**Set-up needed outside the code:**"] + [f"- {CHECKLIST_TEXT.get(k, k)}" for k in sorted(ctx.checklist) if k in CHECKLIST_TEXT]
    nb_cells.append(_nb_cell("markdown", summary, "a2f-summary"))
    if ctx.params:
        pl = ["# Parameters cell: in Fabric toggle this cell as a parameter cell (cell menu > Toggle parameter cell).",
              "# Pipeline-supplied values arrive as strings. Converted from dbutils.widgets."]
        pl += [f"{k} = {v}" for k, v in ctx.params.items()]
        nb_cells.append(_nb_cell("code", pl, "a2f-parameters", tags=["parameters"]))
    for idx, kind, lines in out_cells:
        cid = hashlib.sha1(f"{rel_source}:{idx}".encode()).hexdigest()[:12]
        nb_cells.append(_nb_cell(kind, lines, cid))
    return {"nbformat": 4, "nbformat_minor": 5, "cells": nb_cells,
            "metadata": {**CONFIG["notebook_metadata"],
                         "a2f": {"tool": "adb2fabric", "version": tool_version, "source": rel_source}}}


def convert_file(path: Path, root: Path, out_root: Path, write: bool = True, shared_helpers=None) -> NotebookResult:
    rel = path.relative_to(root).as_posix()
    language = "sql" if path.suffix.lower() == ".sql" else "python"
    text = path.read_text(encoding="utf-8", errors="replace")
    shared_helpers = helper_references(root) if shared_helpers is None else shared_helpers
    out_cells, items, ctx, code_lines, manual_lines, warnings = convert_text(
        text, rel, language, standalone=path.stem.casefold() not in shared_helpers
    )
    nb = build_notebook(rel, out_cells, ctx, items, language)
    syn = _syntax_check([(i, (k, l)) for i, k, l in out_cells])
    target = ""
    if write:
        tgt = (out_root / rel).with_suffix(".ipynb")
        if tgt.exists() and tgt.read_text(encoding="utf-8").find(f'"source": "{rel}"') < 0 and path.suffix.lower() == ".sql":
            tgt = tgt.with_name(tgt.stem + "_sql.ipynb")
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
        target = tgt.relative_to(out_root).as_posix()
    return NotebookResult(rel, target, language, len(out_cells), code_lines, manual_lines, items,
                          {k: sorted(v) for k, v in ctx.checklist.items()}, syn, warnings)
