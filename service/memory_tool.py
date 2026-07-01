"""In-process `remember` tool for updating the home memory Markdown file.

The tool only ever takes a fact and a section label from the model - never a
filesystem path - so there is no path-traversal surface. Writes are
serialized with an asyncio.Lock and committed atomically via os.replace.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

SECTIONS = ["Home", "People", "Preferences", "Routines/Facts", "Do-not"]

DEFAULT_MEMORY = "\n".join(f"# {section}\n" for section in SECTIONS)

_lock = asyncio.Lock()


def _memory_path() -> Path:
    workdir = Path(os.environ.get("AGENT_WORKDIR", "."))
    return workdir / "memory" / "home_memory.md"


def _parse_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^#\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            sections.setdefault(current, [])
            continue
        stripped = line.strip()
        if current and stripped.startswith("- "):
            sections[current].append(stripped[2:])
    return sections


def _render_sections(sections: dict[str, list[str]]) -> str:
    parts = []
    for name in SECTIONS:
        parts.append(f"# {name}")
        for fact in sections.get(name, []):
            parts.append(f"- {fact}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


@tool(
    "remember",
    "Persist a durable fact or preference about this home/household into the "
    "memory file under one of the fixed sections: Home, People, Preferences, "
    "Routines/Facts, Do-not. Do not store raw conversation transcripts.",
    {"fact": str, "section": str},
)
async def remember(args: dict) -> dict:
    fact = args["fact"].strip()
    section = args.get("section", "Routines/Facts")
    if section not in SECTIONS:
        section = "Routines/Facts"

    memory_path = _memory_path()
    async with _lock:
        text = memory_path.read_text() if memory_path.exists() else DEFAULT_MEMORY
        sections = _parse_sections(text)
        if fact not in sections[section]:
            sections[section].append(fact)
        new_text = _render_sections(sections)

        memory_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = memory_path.with_suffix(".tmp")
        tmp_path.write_text(new_text)
        os.replace(tmp_path, memory_path)

    return {"content": [{"type": "text", "text": f"Remembered under {section}: {fact}"}]}


memory_server = create_sdk_mcp_server(name="memory", tools=[remember])
