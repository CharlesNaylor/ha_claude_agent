"""External Claude Agent SDK service for the Claude Bridge HA integration."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from memory_tool import memory_server

WORKDIR = Path(os.environ.get("AGENT_WORKDIR", "."))  # contains .claude/skills/
MEMORY = WORKDIR / "memory" / "home_memory.md"
HA_LLAT = os.environ["HA_LLAT"]
HA_MCP_URL = os.environ.get("HA_MCP_URL", "http://homeassistant.local:8123/api/mcp")
SHARED_SECRET = os.environ["BRIDGE_SHARED_SECRET"]

app = FastAPI()


class Turn(BaseModel):
    text: str
    conversation_id: str | None = None
    language: str | None = None


def load_memory() -> str:
    return MEMORY.read_text() if MEMORY.exists() else "# Home memory\n(empty)\n"


def build_options() -> ClaudeAgentOptions:
    memory_text = load_memory()
    return ClaudeAgentOptions(
        cwd=str(WORKDIR),
        setting_sources=["project"],  # REQUIRED to load .claude/skills/
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are a concise home voice assistant. Keep spoken replies short.\n"
                "Use the Home Assistant MCP tools to control and query devices.\n"
                "Persistent knowledge about this home and the user:\n"
                f"<memory>\n{memory_text}\n</memory>\n"
                "When you learn a durable new fact or preference, call the `remember` "
                "tool to update memory. Do NOT store raw conversation transcripts."
            ),
        },
        mcp_servers={
            "home_assistant": {
                "type": "http",
                "url": HA_MCP_URL,
                "headers": {"Authorization": f"Bearer {HA_LLAT}"},
            },
            "memory": memory_server,
        },
        allowed_tools=[
            "Skill",
            "Read",
            "Grep",
            "Glob",
            "WebSearch",  # native web search
            "mcp__home_assistant__*",  # HA device tools
            "mcp__memory__remember",  # durable memory updates
        ],
        permission_mode="acceptEdits",
        max_turns=6,  # cap loop for voice latency
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/converse")
async def converse(turn: Turn, authorization: str = Header(default="")) -> dict:
    if authorization != f"Bearer {SHARED_SECRET}":
        raise HTTPException(401, "unauthorized")
    options = build_options()
    final = ""
    async for message in query(prompt=turn.text, options=options):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            final = message.result
    return {"reply": final}
