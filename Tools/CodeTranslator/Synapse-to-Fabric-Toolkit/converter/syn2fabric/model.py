from __future__ import annotations

from dataclasses import dataclass, field

AUTO, REVIEW, MANUAL = "AUTO", "REVIEW", "MANUAL"
RANK = {AUTO: 1, REVIEW: 2, MANUAL: 3}


@dataclass
class Cell:
    index: int                 # 0-based position in the source notebook
    type: str                  # code | markdown
    lines: list[str]
    tags: list[str] = field(default_factory=list)


@dataclass
class Item:
    """One detected platform-dependent construct."""
    file: str
    cell: int                  # 1-based cell number in the source notebook
    line: int                  # 1-based line within that cell
    rule_id: str
    level: str
    title: str
    original: str
    converted: str
    message: str = ""


@dataclass
class Context:
    checklist: dict[str, set[str]] = field(default_factory=dict)

    def need(self, key: str, detail: str = "") -> None:
        self.checklist.setdefault(key, set())
        if detail:
            self.checklist[key].add(detail)


@dataclass
class NotebookResult:
    source: str                # path relative to the input root
    target: str                # path relative to the output root ("" when not written)
    language: str              # default language of the source notebook
    cells: int
    code_lines: int
    manual_lines: int
    items: list[Item]
    checklist: dict[str, list[str]]
    syntax_errors: list[str]
    warnings: list[str]
    excluded: frozenset = frozenset()

    @property
    def scored(self) -> list[Item]:
        return [i for i in self.items if i.rule_id not in self.excluded]

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
