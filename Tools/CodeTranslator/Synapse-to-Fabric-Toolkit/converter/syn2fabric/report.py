from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Settings
from .model import AUTO, MANUAL, REVIEW, NotebookResult

LEVEL_RANK = {"MANUAL": 3, "REVIEW": 2, "AUTO": 1}
LEVEL_CLASS = {"AUTO": "ok", "REVIEW": "warn", "MANUAL": "bad"}
STATUS_CLASS = {"Converted": "ok", "Converted - review": "warn", "Needs manual work": "bad"}


def pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def totals(results: list[NotebookResult]) -> dict:
    items = [i for r in results for i in r.scored]
    auto = sum(i.level == AUTO for i in items)
    review = sum(i.level == REVIEW for i in items)
    manual = sum(i.level == MANUAL for i in items)
    code_lines = sum(r.code_lines for r in results)
    manual_lines = sum(r.manual_lines for r in results)
    n = len(items)
    converted = sum(r.status == "Converted" for r in results)
    return {
        "notebooks": len(results),
        "converted": converted,
        "notebook_completion": converted / len(results) if results else None,
        "review": sum(r.status == "Converted - review" for r in results),
        "manual": sum(r.status == "Needs manual work" for r in results),
        "constructs": n, "auto": auto, "review_items": review, "manual_items": manual,
        "strict_coverage": 1.0 if not n else auto / n,
        "weighted_coverage": 1.0 if not n else (auto + 0.5 * review) / n,
        "line_coverage": None if not code_lines else 1 - manual_lines / code_lines,
        "code_lines": code_lines, "manual_lines": manual_lines,
    }


def write_reports(results: list[NotebookResult], skipped: list[tuple[str, str]], in_root: Path, out_root: Path, cfg: Settings) -> Path:
    rdir = out_root / "_report"
    rdir.mkdir(parents=True, exist_ok=True)
    t = totals(results)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    checklist_text = cfg.checklist_text
    payload = {"tool": "syn2fabric", "version": __version__, "generated": stamp, "input": str(in_root), "output": str(out_root),
               "rules_file": str(cfg.path), "totals": t, "skipped": [{"file": f, "reason": r} for f, r in skipped],
               "notebooks": [{"source": r.source, "target": r.target, "status": r.status, "language": r.language, "cells": r.cells,
                              "code_lines": r.code_lines, "manual_lines": r.manual_lines,
                              "weighted_coverage": r.weighted_coverage, "strict_coverage": r.strict_coverage,
                              "line_coverage": r.line_coverage, "syntax_errors": r.syntax_errors, "warnings": r.warnings,
                              "checklist": r.checklist, "items": [i.__dict__ for i in r.items]} for r in results]}
    (rdir / "report.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    with open(rdir / "items.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["notebook", "cell", "line", "status", "rule", "construct", "original", "converted", "action"])
        for r in results:
            for i in r.items:
                w.writerow([r.source, i.cell, i.line, i.level, i.rule_id, i.title, i.original, i.converted, i.message])
    with open(rdir / "manual_work.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["notebook", "cell", "line", "rule", "construct", "original", "what to do"])
        for r in results:
            for i in r.items:
                if i.level == MANUAL:
                    w.writerow([r.source, i.cell, i.line, i.rule_id, i.title, i.original, i.message])

    e = html.escape
    rows, details = [], []
    for n, r in enumerate(results):
        cls = STATUS_CLASS[r.status]
        link = f'<a href="../{e(r.target)}">{e(r.target)}</a>' if r.target else "-"
        syntax = "OK" if not r.syntax_errors else f"<b class=bad>{len(r.syntax_errors)} error(s)</b>"
        rows.append(f'<tr data-s="{cls}"><td><a href="#nb{n}">{e(r.source)}</a></td><td><span class="pill {cls}">{e(r.status)}</span></td>'
                    f'<td class="num">{pct(r.weighted_coverage)}</td><td class="num">{r.count(AUTO)}</td><td class="num">{r.count(REVIEW)}</td>'
                    f'<td class="num">{r.count(MANUAL)}</td><td class="num">{pct(r.line_coverage)}</td><td>{syntax}</td><td>{link}</td></tr>')
        its = []
        for i in sorted(r.items, key=lambda x: (-LEVEL_RANK[x.level], x.cell, x.line)):
            c2 = LEVEL_CLASS[i.level]
            conv = (f"<code>{e(i.converted)}</code>" if i.converted and i.converted != i.original
                    else "<span class=muted>unchanged / see message</span>")
            its.append(f'<tr data-s="{c2}"><td class="num">{i.cell}</td><td class="num">{i.line}</td>'
                       f'<td><span class="pill {c2}">{i.level.title()}</span></td><td>{e(i.rule_id)}</td>'
                       f'<td>{e(i.title)}<div class="muted">{e(i.message)}</div></td><td><code>{e(i.original)}</code></td><td>{conv}</td></tr>')
        extra = ""
        if r.syntax_errors:
            extra += '<p class="bad">Syntax check: ' + e("; ".join(r.syntax_errors)) + "</p>"
        if r.warnings:
            extra += '<p class="muted">' + e(" ".join(r.warnings)) + "</p>"
        if r.checklist:
            extra += "<p><b>Set-up needed:</b></p><ul>" + "".join(
                f"<li>{e(checklist_text.get(k, k))} <span class='muted'>{e('; '.join(v))}</span></li>"
                for k, v in sorted(r.checklist.items())) + "</ul>"
        empty = "<tr><td colspan=7 class=muted>No platform-specific constructs found.</td></tr>"
        details.append(f'<details id="nb{n}"><summary>{e(r.source)} <span class="pill {cls}">{e(r.status)}</span> '
                       f'<span class="muted">{r.constructs} constructs</span></summary>{extra}'
                       f'<table class="items"><thead><tr><th>Cell</th><th>Line</th><th>Result</th><th>Rule</th><th>Construct</th>'
                       f'<th>Original</th><th>Converted</th></tr></thead><tbody>{"".join(its) if its else empty}</tbody></table></details>')
    check_all: dict[str, set] = {}
    for r in results:
        for k in r.checklist:
            check_all.setdefault(k, set()).add(r.source)
    checklist_html = "".join(f"<li><b>{e(checklist_text.get(k, k))}</b> <span class='muted'>({len(v)} notebook(s))</span></li>"
                             for k, v in sorted(check_all.items())) or "<li class=muted>None detected.</li>"
    skipped_html = "".join(f"<li><code>{e(f)}</code> <span class='muted'>{e(rs)}</span></li>" for f, rs in skipped) or "<li class=muted>None.</li>"
    completion = t["notebook_completion"]
    gauge = 0 if completion is None else completion * 100
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Synapse to Fabric conversion report</title>
<style>
body{{font-family:'Segoe UI',Arial,sans-serif;margin:0;color:#1a1a1a;background:#fff}} header{{padding:22px 32px;border-bottom:1px solid #e1dfdd}}
h1{{margin:0;font-size:26px;color:#1f4e79}} h2{{color:#1f4e79;font-size:18px;margin:26px 0 8px}} main{{padding:0 32px 40px}} .muted{{color:#6d6e71;font-size:12px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}} .card{{border:1px solid #1f4e79;padding:12px 18px;min-width:150px}}
.card b{{display:block;font-size:26px;color:#0078d4}} .card.bad b{{color:#e94025}} .card.warn b{{color:#b36b00}} .card.ok b{{color:#107c10}}
.gauge{{width:190px;height:190px;border-radius:50%;background:conic-gradient(#107c10 {gauge}%, #e1dfdd 0);display:flex;align-items:center;justify-content:center}}
.gauge div{{width:140px;height:140px;border-radius:50%;background:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.gauge b{{font-size:30px;color:#1f4e79}} .top{{display:flex;gap:34px;align-items:center;flex-wrap:wrap;margin-top:14px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th{{background:#1f4e79;color:#fff;text-align:left;padding:7px 8px}} td{{padding:6px 8px;border-bottom:1px solid #e1dfdd;vertical-align:top}}
.num{{text-align:right}} .pill{{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff;white-space:nowrap}} .pill.ok{{background:#107c10}} .pill.warn{{background:#b36b00}} .pill.bad{{background:#e94025}}
.bad{{color:#e94025}} code{{font-family:Consolas,monospace;font-size:12px;background:#f3f2f1;padding:1px 4px;word-break:break-all}}
details{{border:1px solid #e1dfdd;margin:8px 0;padding:8px 12px}} summary{{cursor:pointer;font-weight:600}} .filters button{{border:1px solid #1f4e79;background:#fff;color:#1f4e79;padding:5px 12px;margin-right:6px;cursor:pointer}}
.filters button.on{{background:#1f4e79;color:#fff}} .note{{background:#dae3f3;padding:10px 14px;font-size:13px;margin:14px 0}}
</style></head><body>
<header><h1>Azure Synapse to Microsoft Fabric: conversion report</h1>
<div class="muted">syn2fabric {__version__} | generated {e(stamp)} | input <code>{e(str(in_root))}</code> | output <code>{e(str(out_root))}</code></div></header>
<main>
<div class="top"><div class="gauge"><div><b>{pct(completion)}</b><span class="muted">notebooks fully converted</span><span class="muted">{t['converted']} of {t['notebooks']} notebooks</span></div></div>
<div class="cards">
<div class="card"><b>{t['notebooks']}</b>notebooks</div>
<div class="card ok"><b>{t['converted']}</b>fully converted</div>
<div class="card warn"><b>{t['review']}</b>converted, review</div>
<div class="card bad"><b>{t['manual']}</b>need manual work</div>
<div class="card"><b>{pct(t['weighted_coverage'])}</b>weighted construct coverage<br><span class="muted">({t['auto']} auto + 0.5 x {t['review_items']} review) / {t['constructs']} constructs</span></div>
<div class="card"><b>{t['constructs']}</b>platform constructs<br><span class="muted">{t['auto']} auto, {t['review_items']} review, {t['manual_items']} manual</span></div>
<div class="card"><b>{pct(t['line_coverage'])}</b>code lines without manual work<br><span class="muted">{t['code_lines'] - t['manual_lines']} of {t['code_lines']}</span></div>
</div></div>
<div class="note"><b>Different metrics, different denominators.</b>
<b>Notebook completion: {pct(completion)}</b> = {t['converted']} fully converted / {t['notebooks']} analysed notebooks.
Only notebooks with status Converted count as complete; review and manual-work notebooks do not. Skipped files are excluded.
<b>Weighted construct coverage: {pct(t['weighted_coverage'])}</b> = ({t['auto']} auto + 0.5 x {t['review_items']} review) / {t['constructs']} Synapse-specific constructs
(strict, auto only: {pct(t['strict_coverage'])}). Constructs are pooled across notebooks, not averaged per notebook.
Platform-neutral Spark code and mechanical cell-magic conversions (%%pyspark, %%sql, %%spark, %%sparkr, %%html, %pip) are not scored.
A notebook with no scored constructs has 100% construct coverage but contributes no constructs to the pooled score.
The code-line metric only measures lines without manual flags; review lines are included.
These are static-analysis metrics, <b>not runtime validation</b>. Even fully converted notebooks may need the listed external setup.
Every notebook must still be imported and executed in Fabric and its results verified.
The CLI --fail-below gate continues to use weighted construct coverage.</div>
<h2>Notebooks</h2>
<div class="filters"><button class="on" data-f="all">All</button><button data-f="ok">Converted</button><button data-f="warn">Review</button><button data-f="bad">Manual work</button></div>
<table id="nbtable"><thead><tr><th>Notebook</th><th>Status</th><th class="num">Weighted construct coverage</th><th class="num">Auto</th><th class="num">Review</th><th class="num">Manual</th><th class="num">Lines without manual flags</th><th>Syntax</th><th>Fabric notebook</th></tr></thead><tbody>
{"".join(rows) or "<tr><td colspan=9 class=muted>No notebooks found.</td></tr>"}</tbody></table>
<h2>Set-up checklist (work outside the code)</h2><ul>{checklist_html}</ul>
<h2>Details per notebook</h2>{"".join(details)}
<h2>Skipped files</h2><ul>{skipped_html}</ul>
</main>
<script>
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
const f=b.dataset.f;document.querySelectorAll('#nbtable tbody tr').forEach(r=>r.style.display=(f==='all'||r.dataset.s===f)?'':'none');}});
</script></body></html>"""
    out = rdir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
