# src/dwf_mcp/cookbook/__init__.py
from __future__ import annotations

from importlib.resources import files
from typing import Any

_DOC_NAMES = ("index", "freq-domain", "time-domain", "protocols", "bench")


def doc_names() -> tuple[str, ...]:
    """Stable cookbook document names (without the .md extension)."""
    return _DOC_NAMES


def read_doc(name: str) -> str:
    """Return a cookbook document's markdown. Raises KeyError for unknown names."""
    if name not in _DOC_NAMES:
        raise KeyError(f"unknown cookbook doc {name!r}")
    return (files(__name__) / f"{name}.md").read_text(encoding="utf-8")


def parse_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    """Split a leading `---`...`---` YAML-ish block from the body. Only `key: value`
    and `key: [a, b]` lists are understood (no nested structures). Returns ({}, text)
    when there is no front matter."""
    text = markdown.removeprefix("﻿")
    if not text.startswith("---"):
        return {}, markdown
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, markdown
    fm: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            fm[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        else:
            fm[key] = raw
    body = "\n".join(lines[end + 1:])
    return fm, body


def recipe_tool_names() -> set[str]:
    """Every tool name referenced in any recipe's `tools:` front matter, across all
    docs. Scans each doc for `---`-fenced front-matter blocks (document- or
    recipe-level) and unions their `tools:` lists.

    Constraint: recipe *bodies* must not use a bare `---` Markdown thematic break —
    fence-pairing treats every `---` line as a front-matter delimiter, so a stray one
    would mis-pair the blocks. Use `***` or `___` for a horizontal rule in recipe prose."""
    names: set[str] = set()
    for name in _DOC_NAMES:
        text = read_doc(name)
        lines = text.splitlines()
        fences = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
        for a, b in zip(fences[0::2], fences[1::2], strict=False):
            block = "\n".join(["---", *lines[a + 1:b], "---", ""])
            fm, _ = parse_front_matter(block)
            tools = fm.get("tools", [])
            if isinstance(tools, list):
                names.update(tools)
    return names
