"""External Claude Agent SDK service for the Claude Bridge HA integration.

The `/converse` endpoint negotiates on the request `Accept` header:

* `application/x-ndjson` (what the HA integration sends) -> stream the answer as
  newline-delimited JSON events (`{"type": "delta", "text": ...}`, then a final
  `{"type": "done"}`), so Home Assistant can push tokens into streaming TTS as
  the agent produces them.
* anything else -> the original single `{"reply": "..."}` JSON object.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

import dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    query,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from memory_tool import memory_server

dotenv.load_dotenv()

# uvicorn only configures its own loggers; without this, INFO from the root
# logger is dropped.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger()

WORKDIR = Path(os.environ.get("AGENT_WORKDIR", "."))  # contains .claude/skills/
MEMORY = WORKDIR / "memory" / "home_memory.md"
HA_LLAT = os.environ["HA_LLAT"]
HA_MCP_URL = os.environ.get("HA_MCP_URL", "http://homeassistant.local:8123/api/mcp")
SHARED_SECRET = os.environ["BRIDGE_SHARED_SECRET"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# The HA MCP server exposes ~80 tools, including admin ones (restart, delete
# entities, backups, HACS). Only these are offered to the voice agent; every
# other HA tool is passed as disallowed so it's removed from the model's context.
HA_ALLOWED_TOOLS = {
    "ha_search",
    "ha_get_state",
    "ha_list_floors_areas",
    "ha_get_history",
    "ha_get_todo",
    "ha_set_todo_item",
    "ha_config_get_calendar_events",
    "ha_get_zone",
    "ha_call_service",  # restricted to CALL_SERVICE_DOMAINS by a PreToolUse hook
}
CALL_SERVICE_DOMAINS = {
    "light",
    "switch",
    "fan",
    "cover",
    "climate",
    "humidifier",
    "water_heater",
    "media_player",
    "vacuum",
    "scene",
    "script",
    "button",
    "input_boolean",
    "input_number",
    "input_select",
    "timer",
    "todo",
}

app = FastAPI()

logger = logging.getLogger()

# Maps a Home Assistant conversation_id to the SDK session id of its last turn,
# so follow-up utterances resume the same agent session (multi-turn context).
_sessions: dict[str, str] = {}


class Turn(BaseModel):
    text: str
    conversation_id: str | None = None
    language: str | None = None


def load_memory() -> str:
    return MEMORY.read_text() if MEMORY.exists() else "# Home memory\n(empty)\n"


_ha_disallowed: list[str] | None = None


async def ha_disallowed_tools() -> list[str]:
    """List HA MCP tools outside HA_ALLOWED_TOOLS (cached after first success).

    If HA can't be listed, disallow every HA tool rather than expose admin ones.
    """
    global _ha_disallowed
    if _ha_disallowed is None:
        try:
            async with streamablehttp_client(
                HA_MCP_URL, headers={"Authorization": f"Bearer {HA_LLAT}"}
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
        except Exception:  # noqa: BLE001
            logger.exception("Could not list HA MCP tools; disabling all of them")
            return ["mcp__home_assistant__*"]
        _ha_disallowed = [
            f"mcp__home_assistant__{t.name}"
            for t in tools
            if t.name not in HA_ALLOWED_TOOLS
        ]
    return _ha_disallowed


async def guard_call_service(
    input_data: dict[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Deny ha_call_service outside device-control domains or as a raw WS command."""
    tool_input = input_data.get("tool_input") or {}
    domain = tool_input.get("domain")
    if tool_input.get("ws_command") or domain not in CALL_SERVICE_DOMAINS:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Service domain {domain!r} is not available to the voice "
                    "assistant. Tell the user you can't do that."
                ),
            }
        }
    return {}


def build_system_prompt() -> str:
    return (
        "You are a voice assistant for a home running Home Assistant. Your replies "
        "are read aloud by text-to-speech.\n\n"
        "Reply in one or two short, plain spoken sentences. No markdown, bullet "
        "points, URLs, or source citations. Everything you write is spoken "
        "verbatim, so say only the answer itself: never comment on tool results, "
        "instructions, formatting, or why you left something out. (Web search "
        "results ask for source links - don't add them, and don't mention them.)\n"
        "Use the Home Assistant tools to query and control devices. Find entity "
        "IDs with ha_search before acting on them, and briefly confirm what you did.\n"
        "For questions that need current information, use WebSearch and answer from "
        "the snippets when possible; use WebFetch only if they're insufficient, and "
        "fetch at most one page per request.\n"
        "If a request is ambiguous, ask one short clarifying question.\n\n"
        "Persistent knowledge about this home and the user:\n"
        f"<memory>\n{load_memory()}\n</memory>\n"
        "When you learn a durable new fact or preference, call the `remember` tool "
        "to update memory. Do NOT store raw conversation transcripts."
    )


def build_options(
    disallowed_tools: list[str],
    include_partial_messages: bool = False,
    resume: str | None = None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        cwd=str(WORKDIR),
        include_partial_messages=include_partial_messages,
        resume=resume,  # resume a prior SDK session for multi-turn continuity
        setting_sources=["project"],  # REQUIRED to load .claude/skills/
        # Custom prompt instead of the claude_code preset, which is written for
        # coding and adds ~20k tokens per turn.
        system_prompt=build_system_prompt(),
        mcp_servers={
            "home_assistant": {
                "type": "http",
                "url": HA_MCP_URL,
                "headers": {"Authorization": f"Bearer {HA_LLAT}"},
            },
            "memory": memory_server,
        },
        # Restrict the built-in tool set. With the full Claude Code set (~30 tools)
        # the CLI defers WebSearch/WebFetch behind ToolSearch, costing an extra
        # turn per tool and pushing simple web questions into max_turns.
        tools=["Skill", "Read", "Grep", "Glob", "WebSearch", "WebFetch"],
        allowed_tools=[
            "Skill",
            "Read",
            "Grep",
            "Glob",
            "WebSearch",  # native web search
            "WebFetch",  # fetch full page content from search results
            "mcp__home_assistant__*",  # narrowed by disallowed_tools below
            "mcp__memory__remember",  # durable memory updates
        ],
        disallowed_tools=disallowed_tools,
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="mcp__home_assistant__ha_call_service",
                    hooks=[guard_call_service],
                )
            ]
        },
        # Wait for MCP servers to connect before the first turn. Otherwise HA's
        # tools arrive late and are deferred behind ToolSearch, costing extra turns.
        env={"MCP_CONNECTION_NONBLOCKING": "false"},
        permission_mode="acceptEdits",
        max_turns=6,  # cap loop for voice latency
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _ndjson(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


async def _stream_events(
    text: str, conversation_id: str | None = None
) -> AsyncIterator[bytes]:
    """Run the agent loop and emit NDJSON delta events as text is generated.

    Partial messages surface the raw Anthropic streaming events; we forward only
    `text_delta`s (spoken text), so thinking blocks and tool-call JSON never
    reach the voice pipeline.
    """
    resume = _sessions.get(conversation_id) if conversation_id else None
    options = build_options(
        await ha_disallowed_tools(), include_partial_messages=True, resume=resume
    )
    try:
        logger.info("Trying prompt: %s", text)
        async for message in query(prompt=text, options=options):
            if not isinstance(message, StreamEvent):  # skip per-token events
                logger.info("Got message: %s", message)
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield _ndjson({"type": "delta", "text": delta["text"]})
            elif isinstance(message, ResultMessage):
                if conversation_id and message.session_id:
                    _sessions[conversation_id] = message.session_id
                if message.subtype != "success":
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
            _stream_events(turn.text, turn.conversation_id),
            media_type="application/x-ndjson",
        )

    # Non-streaming fallback: run the loop and return the whole reply at once.
    resume = _sessions.get(turn.conversation_id) if turn.conversation_id else None
    final = ""
    options = build_options(await ha_disallowed_tools(), resume=resume)
    async for message in query(prompt=turn.text, options=options):
        if isinstance(message, ResultMessage):
            if turn.conversation_id and message.session_id:
                _sessions[turn.conversation_id] = message.session_id
            if message.subtype == "success":
                final = message.result
    return {"reply": final}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SERVER_PORT", "8090")))

