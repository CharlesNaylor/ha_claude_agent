# Building a "Smart" Custom Voice-Assistant LLM Backend for Home Assistant with Anthropic Claude

## TL;DR
- **Build it as a hybrid: a thin Home Assistant `ConversationEntity` custom integration that POSTs each utterance over HTTP to an external FastAPI service running the Anthropic Claude Agent SDK.** The external service holds your Agent Skills (SKILL.md folders), a Markdown memory file injected into the system prompt, and MCP servers (Home Assistant MCP Server for device control, plus web search and any future servers). This cleanly exceeds the built-in "Anthropic" conversation integration, which is locked to HA's Assist tool set and cannot load Skills, persistent memory, or arbitrary MCP servers.
- **Two viable device-control paths exist and you should pick by network topology.** If your agent service runs on your LAN, use the Claude Agent SDK with the HA MCP Server at `http://<ha>:8123/api/mcp` (`type:"http"`, `Authorization: Bearer <long-lived token>`). The raw Messages API `mcp_servers` connector requires a *public* HTTPS URL because it brokers from Anthropic's cloud, so it's the wrong tool for a LAN-only home.
- **Latency is the dominant risk for voice.** A full Claude agent loop with MCP round-trips will routinely take 2–6+ seconds; you must use HA's streaming TTS (shipped in Voice Chapter 10/11, ~0.5s to first audio), keep the agent's tool set small, cap turns, and consider a two-pipeline split (fast local intents for "turn on the light," the Claude agent only for complex queries).

## Key Findings

### The build is necessary because the official integration is deliberately constrained
Home Assistant ships an official **Anthropic** integration (listed as "Anthropic" in the integration list) that adds a Claude-powered conversation agent and AI Task entity. It controls HA through the Assist API over exposed entities, supports prompt caching, extended thinking, and (newer) tool-search. But it is architecturally HA-as-client: HA prepares the tool list and prompt and calls the model. It does not load Anthropic Agent Skills, does not maintain a read/write Markdown memory store, and does not let you attach arbitrary external MCP servers. To get all three, you must run your own agent loop outside HA and bridge it in as a conversation agent — exactly the hybrid the user described.

### 1. Home Assistant conversation agent integration

**The modern API (HA 2025+).** A custom conversation agent is a custom integration that registers a `conversation.ConversationEntity`. The canonical method changed: HA used to promote `async_process`, but as of 2025 the recommended override is **`_async_handle_message(self, user_input: ConversationInput, chat_log: ChatLog) -> ConversationResult`** (the change is backwards compatible, and `async_process` still works). Key objects, from the official developer docs:
- `ConversationEntity` (from `homeassistant.components.conversation`) — base class. Must declare `supported_languages` (a `list[str]` or `"*"`). Optional `ConversationEntityFeature.CONTROL` supported feature flag.
- `ConversationInput` — carries `text` (the user utterance), `context`, `conversation_id` (for multi-turn tracking), `language`, and `continue_conversation`.
- `ChatLog` — lets the entity read history and append messages/tool calls; `async_add_assistant_content_without_tools(AssistantContent(...))` adds a plain reply, and `async_add_delta_content_stream(...)` streams deltas and runs tool calls.
- Response is built with `intent.IntentResponse(language=...)`, `response.async_set_speech("...")`, then returned in `agent.ConversationResult(conversation_id=..., response=response, continue_conversation=False)`.
- Optional `async_prepare(self, language)` to warm up resources.

**Registration / making it selectable in an Assist pipeline.** Implementing the conversation platform (a `conversation.py` that sets up the entity via `async_setup_entry` adding the entity) automatically makes the entity appear under **Settings → Voice Assistants → (pipeline) → Conversation agent**. The user then assigns it. The "Control Home Assistant" toggle and exposed-entities page govern the built-in Assist tool path — but in our design the external service does the controlling via the HA MCP Server, so you can leave HA's own LLM API unused for this agent.

**Minimum file structure.** Yes, it must be a custom_component (custom integration); there is no lighter mechanism for a first-class conversation agent. The minimal layout under `config/custom_components/<domain>/`:
- `manifest.json` — required. Keys: `domain` (must match folder), `name`, `version` (required for custom integrations), `config_flow: true` (requires `config_flow.py`), `requirements` (PyPI deps like the Anthropic SDK if you embed it, though in this design the SDK lives in the external service), `iot_class` (e.g. `cloud_polling`), `documentation`, `codeowners`.
- `__init__.py` — `async_setup_entry` / `async_unload_entry` lifecycle.
- `config_flow.py` — UI setup (collect the external service URL and a shared secret).
- `conversation.py` — the `ConversationEntity` subclass that forwards to the external service.
- (Optional) `strings.json` + `translations/en.json`, `hacs.json` if distributing via HACS.

**Forwarding the utterance to an external HTTP service.** In `_async_handle_message`, use HA's shared aiohttp session (`homeassistant.helpers.aiohttp_client.async_get_clientsession`) to POST `{text, conversation_id, language}` to your FastAPI endpoint, await the JSON reply, and set it as speech. Because HA's voice pipeline now supports streaming TTS, the highest-quality approach is to stream tokens back from your service and feed them via `async_add_delta_content_stream`, but a simple non-streaming POST→reply is the easiest correct starting point.

**Newer/cleaner alternatives, compared.** This area is evolving fast — flag accordingly:
- **Extended OpenAI Conversation (jekalmin)** and **hasscc/ai-conversation** are community custom components that expose an OpenAI-compatible conversation agent with function-calling. If your external service exposes an **OpenAI-compatible `/v1/chat/completions` endpoint**, you can point one of these at it and skip writing your own integration entirely — a very common 2025-2026 bridge pattern. Trade-off: you adopt their config surface and their function-calling conventions instead of writing Python.
- **OpenRouter** became an official native HA integration in the **2025.8 "summer of AI" release** (added by @joostlek), giving "Access over 400 different large language models through the OpenRouter API" — incl. Claude — through one key (HA reports the integration is "used by 2165 active installations"). But it's still HA-as-client (no Skills/memory/arbitrary MCP).
- **A bespoke `ConversationEntity`** (this guide's recommendation) gives you full control of streaming, memory injection, and the response contract, at the cost of maintaining ~4 Python files.
- Community projects worth studying as references: `aradlein/hass-agent-llm` (conversation platform + memory extraction + tools), `michelle-avery/custom-conversation` (LiteLLM-backed, multi-provider, Langfuse tracing), and the community "custom component to enable an external conversation agent" forum thread (the exact "JSON in / JSON out HTTP endpoint" pattern).
- A simpler-but-limited alternative: an **automation calling the `conversation.process` action** and routing text to your service, but this does not make a first-class selectable agent for satellites.

### 2. The Home Assistant MCP Server integration

**What it is.** Introduced in **HA 2025.2**, the **Model Context Protocol Server** integration (`mcp_server`, maintained by allenporter) turns HA into an MCP *server* so an external LLM application (the MCP *client*) can control HA. It implements the **Streamable HTTP transport (stateless)** and is exposed at **`/api/mcp`** (full: `http://<ha>:8123/api/mcp`). It is the inverse of the Anthropic conversation integration.

**What it exposes.** It surfaces the **Assist / LLM API tool set**: the full set of Home Assistant intents as tools, plus `GetLiveContext`. Verified tool names (from HA core debug logs and the docs) include: `HassTurnOn`, `HassTurnOff`, `HassSetPosition`, `HassLightSet`, `HassClimateSetTemperature`, `HassSetVolume`, `HassMediaPause`/`HassMediaUnpause`/`HassMediaNext`/`HassMediaPrevious`/`HassMediaSearchAndPlay`, `HassListAddItem`/`HassListCompleteItem`, `todo_get_items`, `HassBroadcast`, `HassCancelAllTimers`, and `GetLiveContext` ("Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas"). The list is **dynamic** — it reflects which entities/domains are exposed (via the exposed-entities page) and grows to include custom intents and exposed scripts. When `GetLiveContext` is present, HA also exposes a read-only MCP **Resource** `homeassistant://assist/context-snapshot`. Supported MCP features: Tools, Prompts, Resources (Assist only); **not** Sampling or Notifications. No administrative tasks are possible — it's scoped to Assist.

**Authentication.** The client must provide an auth token. Two methods: **OAuth (IndieAuth)** — the Client ID is the *client app's* base URL (e.g. `https://claude.ai`), Client Secret unused; or a **Long-Lived Access Token** passed as `Authorization: Bearer <token>`. The docs explicitly say "Some MCP clients may not support OAuth, but may support access tokens. You may create a Long-lived access token to allow the client to access the API." A wrong token yields `401 Unauthorized` on `/api/mcp`; a missing integration yields `404`.

**How the external Anthropic agent connects (as MCP client).** Because the Claude Agent SDK maintains the MCP connection *from your application host*, a LAN-reachable HA works directly:
```python
mcp_servers = {
  "home_assistant": {
    "type": "http",
    "url": "http://homeassistant.local:8123/api/mcp",
    "headers": {"Authorization": f"Bearer {HA_LLAT}"},
  }
}
```
Tools then surface to the agent as `mcp__home_assistant__HassTurnOn`, etc., and must be allow-listed (e.g. `allowed_tools=["mcp__home_assistant__*"]`). Note: HA's stdio-oriented examples use `mcp-proxy`, but a direct HTTP client (the SDK) needs no proxy.

### 3. Anthropic Claude Agent SDK + Agent Skills + MCP

**The SDK.** The **Claude Agent SDK** (Python: `pip install claude-agent-sdk`, requires Python ≥3.10; TS: `@anthropic-ai/claude-agent-sdk`) was renamed from the **Claude Code SDK** and launched September 29, 2025 alongside Claude Sonnet 4.5. It bundles the Claude Code CLI in the package. Two entry points: `query(prompt, options)` (one-shot/streaming async iterator — Claude owns the agent loop) and `ClaudeSDKClient` (bidirectional, stateful multi-turn, also enables in-process custom tools and hooks). Configuration is via `ClaudeAgentOptions(...)`. This is distinct from the lower-level Anthropic Client SDK (`anthropic` package / Messages API) where *you* own the loop.

**How it loads Agent Skills.** A Skill is a directory containing a `SKILL.md` with YAML frontmatter (`name`, `description` required; `name` ≤64 chars lowercase/hyphens matching the folder; `description` ≤1024 chars) plus a Markdown body, with optional `scripts/`, `references/`, `assets/` subfolders. **Progressive disclosure** has three levels: at startup only each skill's name+description (~100 tokens) load into the system prompt; the full SKILL.md body (recommended <500 lines / <5000 tokens) loads only when Claude judges the skill relevant; deeper reference files load on demand. In the SDK, Skills are **filesystem artifacts** discovered automatically — there is no programmatic registration API. You must:
- Put skills under `.claude/skills/<skill-name>/SKILL.md` in (or above) the agent's `cwd`.
- Set `setting_sources=["project"]` (and/or `"user"`) — **required**; without it the SDK loads no filesystem settings/skills.
- Include `"Skill"` in `allowed_tools` (and `Read`/`Bash`/`Glob` as needed). Filter with the `skills=` option (`"all"`, a name list, or `[]`). Note `skills` is a context filter, not a sandbox — unlisted skill files remain readable via Read/Bash.

The alternative is the **Messages API container approach**: upload skills via `client.beta.skills.create(...)` (beta header `skills-2025-10-02`) and reference them with the `container` parameter (up to 8 skills/request) — relevant only if you use raw Messages API + code execution rather than the Agent SDK.

**Attaching MCP servers.** Two layers:
- **Via the Agent SDK** (`ClaudeAgentOptions.mcp_servers`, a discriminated union): stdio (`{"type":"stdio","command":...,"args":[...],"env":{...}}`), remote streamable-HTTP (`{"type":"http","url":...,"headers":{...}}`), SSE (`{"type":"sse","url":...,"headers":{...}}`), and in-process SDK servers via `create_sdk_mcp_server(name, tools=[...])` with `@tool`-decorated functions. Tool names follow `mcp__<server>__<tool>`; you grant access via `allowed_tools` (wildcards like `mcp__github__*` allowed). The SDK does not run OAuth — pass tokens via `headers`.
- **Via the raw Messages API `mcp_servers` connector** (beta header **`mcp-client-2025-11-20`**; the older `mcp-client-2025-04-04` is deprecated): each server is `{"type":"url","url":"https://.../sse","name":...,"authorization_token":"..."}` paired with a `tools` entry `{"type":"mcp_toolset","mcp_server_name":...}`. **Critical limitation:** the server "must be publicly exposed through HTTP" and the `url` "must start with `https://`" because the connection is brokered from Anthropic's cloud — local STDIO and LAN-only/localhost servers cannot be reached. You pre-acquire any OAuth token and pass it as `authorization_token`.

This is the decisive architectural fork for the HA MCP Server: **use the Agent SDK path for a LAN-only HA**; use the Messages API connector only if HA is internet-exposed (Nabu Casa Cloud or TLS reverse proxy).

**Native web search vs. web-search MCP, compared.**
- **Anthropic's native `web_search` server tool** (GA on the Messages API since Sept 10, 2025; current versioned types are `web_search_20250305` and the newer `web_search_20260209` with dynamic filtering, supported on Claude Opus 4.6/Sonnet 4.6/Sonnet 4.5/Opus 4.5/Opus 4.1/Opus 4/Sonnet 4/Haiku 4.5) runs server-side on Anthropic's infrastructure and returns cited results. Per Simon Willison (May 7, 2025) it is "presumably still powered by Brave… charged at $10 per 1,000 searches, which is a little more expensive than what the Brave Search API charges ($3 or $5 or $9 per thousand depending on how you're using them)"; note errored searches are not billed and per-search all-in cost "typically range[s] from $0.02-0.05 per search depending on model choice." It supports `max_uses`, `allowed_domains`/`blocked_domains`, and automatic citations. In the Agent SDK the equivalent built-in is the `WebSearch` tool.
- **A web-search MCP server** (e.g., a fetch server or a hosted search MCP) gives you provider choice and self-hosting but you run/secure it and pay that provider.
- **Recommendation:** use the native web search tool (or the SDK's built-in `WebSearch`) for simplicity and citations; reserve MCP search for when you need a specific provider or on-prem control.

**File read/write tools for memory.** Two clean options:
- The Agent SDK's built-in `Read`/`Write`/`Edit`/`Bash` tools (allow-list them; gate writes with a `can_use_tool` callback or a `PreToolUse` hook to confine writes to the memory directory).
- Anthropic's purpose-built **memory tool** (`{"type":"memory_20250818","name":"memory"}`, generally available on Claude 4+, no beta header) — a *client-side* tool where Claude issues `view`/`create`/`str_replace`/`insert`/`delete`/`rename` commands against a `/memories` directory and **your handler executes them locally** (subclass `BetaAbstractMemoryTool` / use `BetaLocalFilesystemMemoryTool(base_path=...)`). Your handler **must reject paths outside `/memories`** (path-traversal protection). Per Anthropic's "Managing context on the Claude Developer Platform" announcement, "combining the memory tool with context editing improved performance by 39% over baseline. Context editing alone delivered a 29% improvement," and context editing reduced "token consumption by 84%" in their 100-turn web-search evaluation.

### 4. Memory pattern (2025-2026 best practices)

The state of the art strongly favors **simple Markdown files over vector databases** for this use case. Per Letta's "Benchmarking AI Agent Memory: Is a Filesystem All You Need?", "Letta Filesystem scores 74.0% on the LoCoMo benchmark by simply storing conversational histories in a file, beating out specialized memory tool libraries." Anthropic's own Claude Code uses file-based `CLAUDE.md` memory; and Anthropic's "Effective context engineering" guidance recommends "just-in-time" retrieval via lightweight file references rather than pre-loading everything. Concrete recommendations:
- **Structure the file** with clear Markdown sections and stable headings, e.g. `# Home` (areas, devices, entity aliases), `# People` (household members, preferences), `# Preferences` (units, default brightness, quiet hours), `# Routines/Facts`, `# Do-not` (constraints). Keep it lean — large monolithic memory degrades retrieval ("fading memory" / signal lost in noise) and consumes the attention budget.
- **Injection at conversation start.** Read the memory file and inject it into the system prompt. With the Agent SDK, the cleanest method is `system_prompt={"type":"preset","preset":"claude_code","append": memory_text}` (preserves tool behavior, layers your content) or a fully custom system-prompt string for a non-coding assistant persona. Note: a `CLAUDE.md` placed in the project and loaded via `setting_sources=["project"]` is injected into the *conversation*, not the system prompt — fine for memory, but `append` is more explicit and cache-friendly. (`exclude_dynamic_sections=True` keeps the cached prefix stable across sessions.)
- **Letting the model propose updates safely.** Prefer an explicit, dedicated mechanism over free-form file writes: either (a) the Anthropic **memory tool** with its constrained command set and a path-validated handler, or (b) a custom in-process `@tool` like `remember(fact, section)` that appends/updates a specific section. Use a strong system-prompt instruction ("Do NOT store raw conversation history; record only durable facts and preferences; confirm before overwriting"). Anthropic's long-running-agents guidance found models are *less* likely to inappropriately overwrite **JSON** than Markdown — so if accidental clobbering is a concern, a structured JSON memory updated via a tool is safer than letting the model rewrite Markdown wholesale.
- **Concurrency / file locking.** A long-running FastAPI service can handle overlapping voice requests, so guard the memory file: serialize writes with an `asyncio.Lock` (single process) and/or use atomic write-and-rename (`os.replace`) so readers never see a half-written file; for multi-process use OS file locks (`fcntl`/`portalocker`). Keep a timestamped backup or git-commit the memory file on each update for recoverability (a pattern Anthropic uses for long-running agents).

### 5. Putting it together

**End-to-end architecture (in words).**
Voice satellite (e.g. Voice PE / ESP32) → wake word + STT (Whisper/Speech-to-Phrase) → **HA Assist pipeline** → **custom `ConversationEntity`** (your `custom_component`) → **HTTP POST** (utterance + conversation_id) → **external FastAPI agent service** running the **Claude Agent SDK** loop, which has: **[MCP] HA MCP Server** (`/api/mcp`, device control) + **web search** (native tool or MCP) + future MCP servers; **[Agent Skills]** under `.claude/skills/`; **[Markdown memory]** injected into the system prompt and updated via a memory/`remember` tool → response text returned over HTTP → HA **streaming TTS** (Piper/Cloud) → satellite speaks.

**Skeleton: HA `conversation.py` (forwarding entity).**
```python
# custom_components/claude_bridge/conversation.py
from __future__ import annotations
import aiohttp
from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntity, ConversationInput, ConversationResult, ChatLog, AssistantContent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import DOMAIN, CONF_URL, CONF_SECRET

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ClaudeBridgeAgent(entry)])

class ClaudeBridgeAgent(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = "Claude Bridge"

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self):
        return "*"

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log: ChatLog
    ) -> ConversationResult:
        session = async_get_clientsession(self.hass)
        payload = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "language": user_input.language,
        }
        headers = {"Authorization": f"Bearer {self.entry.data[CONF_SECRET]}"}
        try:
            async with session.post(
                self.entry.data[CONF_URL], json=payload, headers=headers, timeout=30
            ) as resp:
                data = await resp.json()
                reply = data["reply"]
        except Exception as err:  # noqa: BLE001
            reply = f"Sorry, the assistant service failed: {err}"

        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(agent_id=user_input.agent_id, content=reply)
        )
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(reply)
        return ConversationResult(
            conversation_id=user_input.conversation_id, response=response,
            continue_conversation=False,
        )
```
`manifest.json`:
```json
{
  "domain": "claude_bridge",
  "name": "Claude Bridge",
  "version": "0.1.0",
  "config_flow": true,
  "iot_class": "cloud_polling",
  "integration_type": "service",
  "codeowners": ["@you"],
  "documentation": "https://github.com/you/claude-bridge",
  "requirements": []
}
```

**Skeleton: external FastAPI agent service (Claude Agent SDK).**
```python
# service/main.py
import os, asyncio
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

WORKDIR = Path(os.environ["AGENT_WORKDIR"])          # contains .claude/skills/
MEMORY = WORKDIR / "memory" / "home_memory.md"
HA_LLAT = os.environ["HA_LLAT"]
HA_MCP_URL = os.environ.get("HA_MCP_URL", "http://homeassistant.local:8123/api/mcp")
SHARED_SECRET = os.environ["BRIDGE_SHARED_SECRET"]
_mem_lock = asyncio.Lock()

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
        setting_sources=["project"],          # REQUIRED to load .claude/skills/
        system_prompt={
            "type": "preset", "preset": "claude_code",
            "append": (
                "You are a concise home voice assistant. Keep spoken replies short.\n"
                "Use the Home Assistant MCP tools to control and query devices.\n"
                "Persistent knowledge about this home and the user:\n"
                f"<memory>\n{memory_text}\n</memory>\n"
                "When you learn a durable new fact or preference, call the memory tool "
                "to update /memories. Do NOT store raw conversation transcripts."
            ),
        },
        mcp_servers={
            "home_assistant": {
                "type": "http", "url": HA_MCP_URL,
                "headers": {"Authorization": f"Bearer {HA_LLAT}"},
            },
        },
        allowed_tools=[
            "Skill", "Read", "Grep", "Glob",
            "WebSearch",                       # native web search
            "mcp__home_assistant__*",          # HA device tools
        ],
        permission_mode="acceptEdits",
        max_turns=6,                           # cap loop for voice latency
    )

@app.post("/converse")
async def converse(turn: Turn, authorization: str = Header(default="")):
    if authorization != f"Bearer {SHARED_SECRET}":
        raise HTTPException(401, "unauthorized")
    options = build_options()
    final = ""
    async for message in query(prompt=turn.text, options=options):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            final = message.result
    return {"reply": final}
```
*(For the constrained memory tool instead of free-form Read/Write, swap to the lower-level `anthropic` client with `BetaLocalFilesystemMemoryTool(base_path=str(MEMORY.parent))` and a path-validated handler. For real voice deployments, stream tokens and feed `chat_log.async_add_delta_content_stream` on the HA side.)*

**Configuration details.**
- Env vars (service): `ANTHROPIC_API_KEY`, `HA_LLAT` (HA long-lived token for MCP), `HA_MCP_URL`, `AGENT_WORKDIR`, `BRIDGE_SHARED_SECRET`.
- HA side: install the **Model Context Protocol Server** integration, enable "Control Home Assistant," and expose only the entities the agent should touch (exposed-entities page). Create the long-lived token under Profile → Security.
- Filesystem: `AGENT_WORKDIR/.claude/skills/<skill>/SKILL.md`; `AGENT_WORKDIR/memory/home_memory.md`.
- **Docker:** package the FastAPI service + Claude Agent SDK. Because the SDK bundles the Claude Code CLI (needs Node.js), base on an image with both Python ≥3.10 and Node ≥18, mount the skills + memory directories as volumes, and put it on the same LAN as HA so it can reach `:8123/api/mcp`. Example: `docker run -e ANTHROPIC_API_KEY -e HA_LLAT -e BRIDGE_SHARED_SECRET -v ./agent:/agent -p 8088:8088 claude-home-agent`.

**Pitfalls, latency, security.**
- **Latency.** The full STT→agent→TTS loop is the make-or-break. Community guidance: a naive build is 3–5s; Alexa-class is ~400–700ms; HA streaming TTS (added in Voice Chapter 10, June 2025) cut local TTS time-to-first-audio roughly 9.5x (from ~5.3s to ~0.56s with Piper). Mitigations: keep exposed entities small (≈30 entities ≈1,300 tokens); `max_turns` cap; prefer Sonnet/Haiku over Opus; consider two pipelines/wake-words (local intents for simple commands, Claude only for hard questions); enable prompt caching. The agent's MCP tool round-trips add seconds each — minimize tool count and lean on `GetLiveContext` rather than many small queries.
- **Other pitfalls.** `setting_sources=["project"]` is required or Skills silently don't load; MCP tools must be explicitly allow-listed (`mcp__server__*`); the Messages API connector cannot reach LAN HA (use the SDK path); HA's MCP server only exposes Assist-scoped tools (no admin); large memory files degrade quality; the SDK CLI needs Node in your container.
- **Security.** The external service is a privileged bridge to your home — authenticate the HA→service hop with a shared secret/mTLS and never expose the service to the internet unauthenticated. Store the `ANTHROPIC_API_KEY` and HA long-lived token as secrets/env vars, never in code or logs. The HA long-lived token grants Assist-level control — scope exposed entities tightly. Path-validate all memory writes (reject anything outside the memory dir) to prevent traversal. If you must expose HA publicly for the Messages API connector, use TLS + a reverse proxy and consider Nabu Casa Cloud. Treat community Skills as prompt-injection vectors — review SKILL.md and any scripts before installing.

## Recommendations
1. **Start with the bespoke `ConversationEntity` bridge + Agent SDK + HA MCP Server on the LAN.** This is the cleanest match to the requirements (Skills + memory + arbitrary MCP). Get a non-streaming POST→reply working end-to-end first, then add streaming.
2. **Use the Agent SDK path, not the Messages API connector,** unless your HA is already internet-exposed. The connector's public-HTTPS requirement is disqualifying for a typical local home.
3. **Use native web search (`WebSearch`/`web_search` tool)** initially; add a search MCP only if you need a specific provider.
4. **Adopt file-based Markdown memory** with an explicit `remember`/memory tool and path validation; inject via system-prompt `append`. Keep it lean; back it up on each write.
5. **Engineer for latency from day one:** streaming TTS, small exposed-entity set, `max_turns`, Sonnet/Haiku, prompt caching, and a two-pipeline split so trivial commands never hit the cloud agent.
6. **Benchmarks that would change the plan:** if median voice round-trip exceeds ~3s, move more commands to the local-intent pipeline and reduce tool count; if costs climb, switch the default model to Haiku and enable full prompt caching; if memory retrieval degrades, split memory into a small always-injected core file plus on-demand reference files (progressive disclosure).

## Caveats
- **Fast-moving area.** Model IDs (e.g. Sonnet 4.5/4.6, Opus 4.x), beta headers (`mcp-client-2025-11-20` superseding `mcp-client-2025-04-04`; `skills-2025-10-02`), and tool version strings (`web_search_20260209`) change frequently; verify against current Anthropic and HA docs before shipping. Some model IDs surfaced in search results (e.g. "claude-opus-4-8") appear in third-party/forward-looking pages and should be confirmed against the official model list.
- **HA MCP Server tool list is dynamic**, not a fixed contract — it depends on exposed entities, custom intents, and scripts; don't hardcode assumptions about which tools exist.
- The Messages API MCP connector is **not covered by Zero Data Retention** and retains data per Anthropic's standard policy — relevant if privacy is a goal of going local.
- The Agent SDK is governed by Anthropic's Commercial Terms; individual/hobbyist API use is permitted but billed — monitor costs in the Anthropic console.
- Streaming the agent's output through HA's `async_add_delta_content_stream` requires more careful implementation than the simple skeleton shown; the tool-call streaming contract is evolving in HA core.
- HA's own "Anthropic" integration may close some of this gap over time (it already added tool-search); re-evaluate before committing to the custom build if your needs are modest.