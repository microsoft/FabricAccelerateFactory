from __future__ import annotations

import re

from .config import CONFIG
from .model import DBTITLE, HEADER, MAGIC, SEPARATOR, Cell

MAGIC_KINDS = [(re.compile(entry["pattern"], re.I), entry["kind"])
               for entry in CONFIG["magic_commands"] if "pattern" in entry]


def parse_export(text: str, default_language: str) -> tuple[list[Cell], list[str]]:
    """Parse a Databricks source export (.py or .sql). Returns (cells, warnings)."""
    warnings: list[str] = []
    raw = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    start = 0
    if raw and HEADER.match(raw[0]):
        start = 1
    else:
        warnings.append("No 'Databricks notebook source' header; treated the whole file as one cell.")
    groups: list[tuple[int, list[str]]] = []
    cur: list[str] = []
    cur_start = start + 1
    for n in range(start, len(raw)):
        if SEPARATOR.match(raw[n]):
            groups.append((cur_start, cur))
            cur, cur_start = [], n + 2
        else:
            cur.append(raw[n])
    groups.append((cur_start, cur))

    cells: list[Cell] = []
    for idx, (ln, lines) in enumerate(groups):
        title, body = "", []
        for line in lines:
            m = DBTITLE.match(line)
            if m and not body:
                title = m.group(1).strip()
            else:
                body.append(line)
        while body and not body[0].strip():
            body.pop(0)
            ln += 1
        while body and not body[-1].strip():
            body.pop()
        if not body:
            continue
        nonblank = [b for b in body if b.strip()]
        if all(MAGIC.match(b) for b in nonblank):
            stripped = [(MAGIC.match(b).group(1) or "") if MAGIC.match(b) else "" for b in body]
            i0 = next(i for i, s in enumerate(stripped) if s.strip())
            first = stripped[i0].strip()
            kind = "unknown_magic"
            for rx, k in MAGIC_KINDS:
                if rx.match(first):
                    kind = k
                    break
            keyword = first.split()[0] if first.split() else ""
            if kind in ("run", "fs", "pip", "unknown_magic"):
                body_lines = stripped[i0:]                      # command lives on the magic line
            else:
                remainder = re.sub(r"^%\S+\s*", "", stripped[i0], count=1)
                body_lines = ([remainder] if remainder.strip() else []) + stripped[i0 + 1:]
            cells.append(Cell(idx, ln + i0, kind, body_lines, keyword, title))
        else:
            cells.append(Cell(idx, ln, "sql" if default_language == "sql" else "code", body, "", title))
    return cells, warnings
