from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import CONFIG

AUTO, REVIEW, MANUAL = "AUTO", "REVIEW", "MANUAL"
RANK = {AUTO: 1, REVIEW: 2, MANUAL: 3}
# Cell-magic conversions that are mechanical and carry no migration risk: listed in the report, excluded from the score.
TRIVIAL_RULES = set(CONFIG["excluded_rule_ids"])
SEPARATOR = re.compile(r"^(?:#|--)\s*COMMAND\s*-{3,}\s*$")
HEADER = re.compile(r"^(?:#|--)\s*Databricks notebook source\s*$")
MAGIC = re.compile(r"^(?:#|--)\s*MAGIC(?: (.*)|\s*)$")
DBTITLE = re.compile(r"^(?:#|--)\s*DBTITLE\s+\d+,(.*)$")


@dataclass
class Cell:
    index: int
    start_line: int            # 1-based line in the source file
    kind: str                  # code | sql | markdown | scala | r | sh | run | fs | pip | unknown_magic
    lines: list[str]           # body without the MAGIC prefix / magic keyword
    magic: str = ""            # original magic keyword, e.g. "%sql"
    title: str = ""


@dataclass
class Item:
    """One detected platform-dependent construct."""
    file: str
    cell: int
    line: int
    rule_id: str
    level: str
    title: str
    original: str
    converted: str
    message: str = ""


@dataclass
class Context:
    params: dict[str, str] = field(default_factory=dict)       # widget name -> python literal default
    checklist: dict[str, set[str]] = field(default_factory=dict)

    def need(self, key: str, detail: str = "") -> None:
        self.checklist.setdefault(key, set())
        if detail:
            self.checklist[key].add(detail)


@dataclass
class NotebookResult:
    source: str                # path relative to input root
    target: str                # path relative to output root ("" when not written)
    language: str              # python | sql
    cells: int
    code_lines: int
    manual_lines: int
    items: list[Item]
    checklist: dict[str, list[str]]
    syntax_errors: list[str]
    warnings: list[str]

    @property
    def scored(self) -> list[Item]:
        return [i for i in self.items if i.rule_id not in TRIVIAL_RULES]

    def count(self, level: str) -> int:
        return sum(1 for i in self.scored if i.level == level)

    @property
    def constructs(self) -> int:
        return len(self.scored)

    @property
    def strict_coverage(self) -> float:
        return 1.0 if not self.scored else self.count(AUTO) / len(self.scored)

    @property
    def weighted_coverage(self) -> float:
        return 1.0 if not self.scored else (self.count(AUTO) + 0.5 * self.count(REVIEW)) / len(self.scored)

    @property
    def line_coverage(self) -> float | None:
        return None if not self.code_lines else 1 - self.manual_lines / self.code_lines

    @property
    def status(self) -> str:
        if self.count(MANUAL) or self.syntax_errors:
            return "Needs manual work"
        if self.count(REVIEW):
            return "Converted - review"
        return "Converted"
