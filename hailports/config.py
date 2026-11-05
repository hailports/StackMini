"""Small YAML subset reader and writer. Enough for our config files."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if s in ("null", "None", "~", ""):
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def loads(text: str) -> dict[str, Any]:
    """Parse nested mappings and '- item' lists. Two-space indent."""
    rows: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((lineno, indent, line.strip()))
    index = 0

    def parse_map(min_indent: int) -> dict[str, Any]:
        nonlocal index
        mapping: dict[str, Any] = {}
        while index < len(rows):
            lineno, indent, body = rows[index]
            if indent < min_indent:
                break
            if body.startswith("- "):
                raise ValueError(f"line {lineno}: list item outside a list")
            if ":" not in body:
                raise ValueError(f"line {lineno}: expected key")
            key, _, rest = body.partition(":")
            key = key.strip()
            rest = rest.strip()
            index += 1
            if rest in ("", "[]") and rest != "[]":
                if index < len(rows) and rows[index][1] > indent and rows[index][2].startswith("- "):
                    items: list[Any] = []
                    while index < len(rows) and rows[index][1] > indent and rows[index][2].startswith("- "):
                        items.append(_parse_scalar(rows[index][2][2:]))
                        index += 1
                    mapping[key] = items
                else:
                    mapping[key] = parse_map(indent + 1)
            elif rest == "[]":
                mapping[key] = []
            else:
                mapping[key] = _parse_scalar(rest)
        return mapping

    return parse_map(0)


def _dump(node: Any, indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                lines.append(f"{pad}{key}:")
                lines.extend(_dump(value, indent + 2))
            elif isinstance(value, list):
                if not value:
                    lines.append(f"{pad}{key}: []")
                else:
                    lines.append(f"{pad}{key}:")
                    for item in value:
                        rendered = _scalar(item)
                        lines.append(f"{pad}  - {rendered}")
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
        return lines
    raise TypeError("root must be a mapping")


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(c in text for c in ":#[]{}"):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def dumps(data: dict[str, Any]) -> str:
    return "\n".join(_dump(data, 0)) + "\n"


def load_file(path: Path) -> dict[str, Any]:
    return loads(path.read_text(encoding="utf-8"))


def save_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data), encoding="utf-8")


PLACEHOLDER_TOKENS = ("CHANGE_ME", "example.invalid")


def is_placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return any(tok in value for tok in PLACEHOLDER_TOKENS) or value.strip() == ""
