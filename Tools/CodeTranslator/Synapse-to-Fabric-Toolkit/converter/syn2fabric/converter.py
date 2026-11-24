from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

from . import __version__
from .config import Settings
from .model import AUTO, MANUAL, REVIEW, Context, Item, NotebookResult
from .reader import SynapseNotebook

TRIPLE = re.compile(r"\"\"\"|'''")
TAG_NAME = "SYN2FABRIC"


def apply_mappings(lines, cfg: Settings, ctx: Context, file, cell_no, start_line):
    """Apply the configured path and table mappings. Returns (lines, {line offset: [Item]})."""
    out, evidence = list(lines), {}
    for off, line in enumerate(lines):
        cur, found = line, []
        for mapping in cfg.path_mappings:
            if mapping["source"] in cur:
                cur = cur.replace(mapping["source"], mapping["target"])
                detail = f"{mapping['id']}: {mapping['source']} -> {mapping['target']}"
                ctx.need("storage", f"Configured path mapping {detail}; verify access and the OneLake target before running, especially delete operations.")
                if "onelake" in mapping["target"].lower() or mapping["target"].startswith(("Files/", "Tables/")):
                    ctx.need("lakehouse", f"Configured path mapping {mapping['id']} targets OneLake or a lakehouse-relative path.")
                found.append(Item(file, cell_no, start_line + off, "P-MAP", AUTO, "Configured Synapse -> Fabric path",
                                  line.strip(), "", detail + "; converted using the enabled mapping. Verify storage setup before execution."))
        for rx, source, target in cfg.table_mappings:
            if rx.search(cur):
                cur = rx.sub(target, cur)
                ctx.need("lakehouse", f"Configured table mapping {source} -> {target}; attach the target lakehouse or use qualified names.")
                found.append(Item(file, cell_no, start_line + off, "P-TBL", AUTO, "Configured table mapping",
                                  line.strip(), "", f"{source} -> {target}; converted using the configured mapping. Verify the target table exists."))
        out[off] = cur
        for item in found:
            item.converted = cur.strip()
        if found:
            evidence[off] = found
    return out, evidence


def _applies(rule, kind: str) -> bool:
    return rule.scope == "any" or (rule.scope == "py" and kind == "code") or (rule.scope == "sql" and kind == "sql")


def process_lines(lines, kind, ctx, cfg: Settings, file, cell_no, start_line):
    """Apply path/table mappings and the rule catalogue line by line.

    Returns (new_lines, items, manual_line_count, code_line_count). kind is code, sql, scala or r.
    """
    items: list[Item] = []
    out: list[str] = []
    deferred: list[str] = []
    manual_lines = code_lines = 0
    cmt = {"sql": "--", "scala": "//"}.get(kind, "#")
    mapped, evidence = apply_mappings(lines, cfg, ctx, file, cell_no, start_line)
    in_str = None
    for off, line in enumerate(lines):
        starts_in_string = in_str is not None
        for token in TRIPLE.findall(line):
            in_str = token if in_str is None else (None if token == in_str else in_str)
        stripped = line.strip()
        if not stripped or (stripped.startswith(cmt) and not starts_in_string):
            out.append(mapped[off])
            continue
        code_lines += 1
        cur = mapped[off]
        line_items = list(evidence.get(off, []))
        for rule in cfg.rules:
            if not _applies(rule, kind):
                continue
            if rule.skip_on_manual and any(i.level == MANUAL for i in line_items):
                continue
            if any(i.rule_id == rule.id for i in line_items) or not rule.rx.search(cur):
                continue
            if rule.checklist:
                ctx.need(*rule.checklist)
            if rule.silent:
                continue
            converted, level, message = rule.apply(cur, ctx, cfg)
            line_items.append(Item(file, cell_no, start_line + off, rule.id, level, rule.title, stripped, converted.strip(), message))
            cur = converted
        for item in line_items:
            if item.rule_id in ("P-MAP", "P-TBL"):
                item.converted = cur.strip()
        items.extend(line_items)
        has_manual = any(i.level == MANUAL for i in line_items)
        if has_manual:
            manual_lines += 1
        tag = ""
        if line_items and f"[{TAG_NAME}]" not in cur:
            worst = MANUAL if has_manual else (REVIEW if any(i.level == REVIEW for i in line_items) else AUTO)
            ids = ",".join(i.rule_id for i in line_items if i.level == worst)
            if worst == MANUAL:
                first = next(i for i in line_items if i.level == MANUAL)
                tag = f"TODO[{TAG_NAME}] {ids}: {first.title}"
            elif worst == REVIEW:
                tag = f"REVIEW[{TAG_NAME}] {ids}"
        if tag:
            safe_inline = (not starts_in_string and in_str is None and not TRIPLE.search(line)
                           and not cur.rstrip().endswith("\\") and not cur.lstrip().startswith(("%", "!")))
            if safe_inline or kind == "sql":
                cur = f"{cur.rstrip()}  {cmt} {tag}"
            else:
                deferred.append(f"{cmt} {tag} (source line {start_line + off}: magic command, continuation or multi-line string)")
        out.append(cur)
    return deferred + out, items, manual_lines, code_lines


def _split_magic(lines):
    """Return (index of the first non-blank line, that line stripped) or (None, '')."""
    for i, line in enumerate(lines):
        if line.strip():
            return i, line.strip()
    return None, ""


def _unsupported_cell(lines, magic_id, title):
    return [f"# TODO[{TAG_NAME}] {magic_id}: {title}; original below"] + ["# " + line for line in lines]


def _syntax_check(cells) -> list[str]:
    errors = []
    for cell_no, kind, lines in cells:
        if kind != "code" or lines and lines[0].lstrip().startswith("%%"):
            continue
        probe = "\n".join("pass" if l.lstrip().startswith(("%", "!")) else l for l in lines)
        try:
            ast.parse(probe)
        except SyntaxError as exc:
            errors.append(f"cell {cell_no}: {exc.msg} (line {exc.lineno})")
    return errors


def convert_cells(nb: SynapseNotebook, rel, cfg: Settings):
    """Returns (output cells, items, context, code lines, manual lines, warnings).

    Each output cell is (source cell number, kind, lines, tags); kind is markdown, code, sql, scala or r.
    """
    ctx = Context()
    items: list[Item] = []
    out = []
    code_lines = manual_lines = 0
    default_kind = {"python": "code", "sql": "sql", "scala": "scala", "r": "r"}.get(nb.language, "csharp")
    if nb.pool:
        ctx.need("pool", f"Spark pool '{nb.pool}'" + (f" ({nb.pool_detail})" if nb.pool_detail else "")
                 + ": use the Fabric Starter pool or create a custom pool or Environment if the workload needs it.")
    sizing = [f"{k}={v}" for k, v in nb.session_properties.items() if k != "conf" and v not in (None, "")]
    conf = nb.session_properties.get("conf")
    if isinstance(conf, dict) and conf:
        sizing.append(f"{len(conf)} Spark propert{'y' if len(conf) == 1 else 'ies'}")
    if sizing:
        ctx.need("sessionconf", "Session settings in the source notebook (" + ", ".join(sizing) + ") are not carried over.")
    for cell in nb.cells:
        cell_no = cell.index + 1
        if cell.type == "markdown":
            out.append((cell_no, "markdown", cell.lines, []))
            continue
        first_i, first = _split_magic(cell.lines)
        if first_i is None:
            continue
        kind, body, body_start, magic_entry, keep_magic = default_kind, cell.lines, 1, None, False
        if first.startswith("%%"):
            magic_entry = next((e for rx, e in cfg.magics if rx is not None and rx.match(first)), cfg.magics[-1][1])
            kind = magic_entry["kind"]
            body, body_start = cell.lines[first_i + 1:], first_i + 2
            keep_magic = kind in ("configure", "html", "passthrough")
        if kind == "csharp":
            entry = magic_entry or {"id": "MAG-CS", "level": MANUAL, "title": "%%csharp (.NET for Spark) cell",
                                    "message": ".NET for Spark (C#/F#) is not supported in Fabric. Rewrite the cell in PySpark or Scala."}
            ctx.need("dotnet", cfg.checklist_text["dotnet"])
            items.append(Item(rel, cell_no, first_i + 1, entry["id"], entry["level"], entry["title"], first, "", entry.get("message", "")))
            out.append((cell_no, "code", _unsupported_cell(cell.lines, entry["id"], entry["title"]), []))
            manual_lines += 1
            code_lines += 1
            continue
        if kind == "unknown":
            entry = magic_entry
            items.append(Item(rel, cell_no, first_i + 1, entry["id"], entry["level"], entry["title"], first, "", entry.get("message", "")))
            out.append((cell_no, "code", _unsupported_cell(cell.lines, entry["id"], entry["title"]), []))
            manual_lines += 1
            code_lines += 1
            continue
        if kind == "configure":
            ctx.need("sessionconf", "%%configure cell: verify the settings against the Fabric session options or move them to an Environment.")
            items.append(Item(rel, cell_no, first_i + 1, magic_entry["id"], magic_entry["level"], magic_entry["title"], first,
                              first, magic_entry.get("message", "")))
            out.append((cell_no, "code", cell.lines[first_i:], cell.tags))
            code_lines += 1
            continue
        if magic_entry is not None:
            retained = keep_magic or kind in ("sql", "scala", "r")
            items.append(Item(rel, cell_no, first_i + 1, magic_entry["id"], magic_entry["level"], magic_entry["title"], first,
                              first if retained else "", magic_entry.get("message", "")))
        if kind in ("html", "passthrough"):
            if kind == "passthrough":
                new, it, ml, cl = process_lines(body, "code", ctx, cfg, rel, cell_no, body_start)
                items.extend(it)
                manual_lines += ml
                code_lines += cl
                out.append((cell_no, "code", [cell.lines[first_i]] + new, cell.tags))
            else:
                out.append((cell_no, "code", cell.lines[first_i:], cell.tags))
            continue
        new, it, ml, cl = process_lines(body, kind, ctx, cfg, rel, cell_no, body_start)
        items.extend(it)
        manual_lines += ml
        code_lines += cl
        out.append((cell_no, kind, new, cell.tags))
    return out, items, ctx, code_lines, manual_lines, nb.warnings


def _nb_cell(kind, lines, cid, cfg: Settings, tags=None):
    source = [l + "\n" for l in lines[:-1]] + ([lines[-1]] if lines else [])
    if kind == "markdown":
        return {"cell_type": "markdown", "id": cid, "metadata": {}, "source": source}
    language, group = cfg.data["cell_metadata"].get(kind, cfg.data["cell_metadata"]["code"])
    meta = {"microsoft": {"language": language, "language_group": group}}
    if tags:
        meta["tags"] = tags
    return {"cell_type": "code", "id": cid, "metadata": meta, "execution_count": None, "outputs": [], "source": source}


def build_notebook(rel, out_cells, ctx, items, cfg: Settings, tool_version=__version__):
    scored = [i for i in items if i.rule_id not in cfg.excluded]
    auto = sum(i.level == AUTO for i in scored)
    review = sum(i.level == REVIEW for i in scored)
    manual = sum(i.level == MANUAL for i in scored)
    summary = [f"### Converted from Azure Synapse Analytics by syn2fabric {tool_version}",
               f"Source: `{rel}`  |  Synapse-specific constructs: **{len(scored)}** (auto {auto}, review {review}, manual {manual})",
               "", f"Search this notebook for `TODO[{TAG_NAME}]` (manual work) and `REVIEW[{TAG_NAME}]` (verify behaviour). "
               "Details are in the conversion report. This is a static conversion: it has not been executed."]
    if ctx.checklist:
        summary += ["", "**Set-up needed outside the code:**"] + [
            f"- {cfg.checklist_text.get(k, k)}" for k in sorted(ctx.checklist) if k in cfg.checklist_text]
    cells = [_nb_cell("markdown", summary, "s2f-summary", cfg)]
    for cell_no, kind, lines, tags in out_cells:
        cid = hashlib.sha1(f"{rel}:{cell_no}".encode()).hexdigest()[:12]
        prefix = cfg.data["cell_prefixes"].get(kind, [])
        cells.append(_nb_cell(kind, prefix + lines if kind != "markdown" else lines, cid, cfg, tags))
    metadata = json.loads(json.dumps(cfg.data["notebook_metadata"]))
    metadata["syn2fabric"] = {"tool": "syn2fabric", "version": tool_version, "source": rel}
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells, "metadata": metadata}


def convert_notebook(nb: SynapseNotebook, path: Path, root: Path, out_root: Path, cfg: Settings, write: bool = True) -> NotebookResult:
    rel = path.relative_to(root).as_posix()
    out_cells, items, ctx, code_lines, manual_lines, warnings = convert_cells(nb, rel, cfg)
    notebook = build_notebook(rel, out_cells, ctx, items, cfg)
    syntax = _syntax_check([(n, k, l) for n, k, l, _ in out_cells])
    target = ""
    if write:
        destination = (out_root / rel).with_suffix(".ipynb")
        if path.suffix.lower() == ".json" and (path.with_suffix(".ipynb")).exists():
            destination = destination.with_name(destination.stem + "_json.ipynb")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf-8")
        target = destination.relative_to(out_root).as_posix()
    checklist = {k: sorted(v) for k, v in ctx.checklist.items()}
    return NotebookResult(rel, target, nb.language, len(out_cells), code_lines, manual_lines, items, checklist,
                          syntax, warnings, cfg.excluded)
