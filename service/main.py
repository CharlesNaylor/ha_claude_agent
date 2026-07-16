"""External Claude Agent SDK service for the Claude Bridge HA integration.

The `/converse` endpoint negotiates on the request `Accept` header:

* `application/x-ndjson` (what the HA integration sends) -> stream the answer as
  newline-delimited JSON events (`{"type": "delta", "text": ...}`, then a final
  `{"type": "done"}`), so Home Assistant can push tokens into streaming TTS as
  the agent produces them.
* anything else -> the original single `{"reply": "..."}` JSON object.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, StreamEvent, query

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


def build_options(include_partial_messages: bool = False) -> ClaudeAgentOptions:
    memory_text = load_memory()
    return ClaudeAgentOptions(
        cwd=str(WORKDIR),
        include_partial_messages=include_partial_messages,
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


def _ndjson(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


async def _stream_events(text: str) -> AsyncIterator[bytes]:
    """Run the agent loop and emit NDJSON delta events as text is generated.

    Partial messages surface the raw Anthropic streaming events; we forward only
    `text_delta`s (spoken text), so thinking blocks and tool-call JSON never
    reach the voice pipeline.
    """
    options = build_options(include_partial_messages=True)
    try:
        async for message in query(prompt=text, options=options):
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield _ndjson({"type": "delta", "text": delta["text"]})
            elif isinstance(message, ResultMessage) and message.subtype != "success":
                yield _ndjson({"type": "error", "message": message.subtype})
    except Exception as err:  # noqa: BLE001
        yield _ndjson({"type": "error", "message": str(err)})
    yield _ndjson({"type": "done"})


@app.post("/converse")
async def converse(
    turn: Turn,
    authorization: str = Header(default=""),
    accept: str = Header(default=""),
):
    if authorization != f"Bearer {SHARED_SECRET}":
        raise HTTPException(401, "unauthorized")

    if "application/x-ndjson" in accept:
        return StreamingResponse(
            _stream_events(turn.text), media_type="application/x-ndjson"
        )

    # Non-streaming fallback: run the loop and return the whole reply at once.
    final = ""
    async for message in query(prompt=turn.text, options=build_options()):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            final = message.result
    return {"reply": final}
