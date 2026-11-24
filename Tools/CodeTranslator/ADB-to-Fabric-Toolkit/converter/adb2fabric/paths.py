from __future__ import annotations

import ast
import io
import re
import token
import tokenize

from .config import CONFIG


FS_METHODS = {"ls", "cp", "mv", "rm", "mkdirs", "head", "put", "append"}
FS_PATH_KEYWORDS = {"path", "file", "file_path", "source", "destination", "src", "dst", "from_path", "to_path"}


def fs_path_arguments(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in FS_METHODS and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "fs" and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id in ("dbutils", "notebookutils")):
        return []
    args = node.args[:2 if node.func.attr in ("cp", "mv") else 1]
    return args + [kw.value for kw in node.keywords if kw.arg in FS_PATH_KEYWORDS]


def _fs_argument_spans(text, lines, offsets):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    spans = []

    def absolute(line, byte_column):
        return offsets[line - 1] + len(lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8"))

    for node in ast.walk(tree):
        for arg in fs_path_arguments(node):
            if isinstance(arg, ast.JoinedStr) or (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                spans.append((absolute(arg.lineno, arg.col_offset), absolute(arg.end_lineno, arg.end_col_offset)))
    return spans


def _literal_spans(text, kind):
    if kind == "code":
        try:
            entries = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # The converter reports syntax errors separately; don't map an incomplete cell.
            return
        for index, entry in enumerate(entries):
            if entry.type == token.STRING or token.tok_name[entry.type] == "FSTRING_MIDDLE":
                middle = token.tok_name[entry.type] == "FSTRING_MIDDLE"
                at_start = middle and index > 0 and token.tok_name[entries[index - 1].type] == "FSTRING_START"
                at_end = not middle or index + 1 < len(entries) and token.tok_name[entries[index + 1].type] == "FSTRING_END"
                yield entry.start, entry.end, at_start, at_end
    elif kind == "sql":
        # Consume comments before literals so paths in SQL comments are never rewritten.
        rx = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`", re.S)
        for match in rx.finditer(text):
            if match.group()[0] in "'\"`":
                start = text.count("\n", 0, match.start())
                end = text.count("\n", 0, match.end())
                yield ((start + 1, match.start() - (text.rfind("\n", 0, match.start()) + 1)),
                       (end + 1, match.end() - (text.rfind("\n", 0, match.end()) + 1)), False, True)


def map_paths(lines, kind):
    """Rewrite configured literal prefixes, returning per-source-line mapping evidence."""
    mappings = [m for m in CONFIG.get("path_mappings", []) if m.get("enabled", True)]
    if not mappings or kind not in ("code", "sql"):
        return lines, {}
    mappings.sort(key=lambda m: len(m["source"].rstrip("/")), reverse=True)
    text = "\n".join(lines)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
    spans = list(_literal_spans(text, kind))
    edits, evidence = [], {}
    scoped_spans = _fs_argument_spans(text, lines, offsets) if kind == "code" and any(
        m.get("scope", "all") == "dbutils_fs" for m in mappings) else []
    for start, end, at_start, at_end in spans:
        a = offsets[start[0] - 1] + start[1]
        b = offsets[end[0] - 1] + end[1]
        candidates = [m for m in mappings if m.get("scope", "all") == "all"
                      or any(begin <= a and b <= finish for begin, finish in scoped_spans)]
        if not candidates:
            continue
        alternatives = "|".join(re.escape(m["source"].rstrip("/")) for m in candidates)
        literal = text[a:b]
        left = r"(?:(?<=['\"`])|^)" if at_start else r"(?<=['\"`])"
        right = r"(?=/|['\"`]|$)" if at_end else r"(?=/|['\"`])"
        rx = re.compile(left + "(?P<prefix>" + alternatives + ")" + right)
        for match in rx.finditer(literal):
            mapping = next(m for m in candidates if m["source"].rstrip("/") == match.group("prefix"))
            edits.append((a + match.start(), a + match.end(), mapping["target"].rstrip("/")))
            line_index = text.count("\n", 0, a + match.start())
            evidence.setdefault(line_index, []).append(mapping)
    for a, b, replacement in reversed(edits):
        text = text[:a] + replacement + text[b:]
    return text.split("\n"), evidence
