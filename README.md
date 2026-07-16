# Claude Bridge for Home Assistant

A thin Home Assistant **conversation agent** that forwards each voice/chat utterance
to an external [Claude Agent SDK](https://docs.anthropic.com/en/api/agent-sdk) service.
The external service holds your Agent Skills, persistent Markdown memory, and MCP
servers (including the Home Assistant MCP Server for device control) — capabilities
the built-in Anthropic integration can't load.

This repo has two halves:

| Path | What it is | Where it runs |
| --- | --- | --- |
| `custom_components/claude_bridge/` | The HA integration (installed via HACS) | Home Assistant |
| `service/` | The FastAPI + Claude Agent SDK backend | Your LAN (Docker/host) |

**HACS installs only the integration.** You must run the `service/` half separately on
your LAN so it can reach `http://<ha>:8123/api/mcp`. See `service/` for details.

## Installation (HACS)

This is not (yet) in the default HACS store, so add it as a custom repository:

1. In Home Assistant, open **HACS → ⋮ (top-right) → Custom repositories**.
2. Enter the repository URL `https://github.com/CharlesNaylor/ha_claude_agent`
   and select type **Integration**.
3. Find **Claude Bridge** in HACS, install it, then **restart Home Assistant**.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **Claude Bridge**.

### Manual installation (alternative)

Copy `custom_components/claude_bridge/` into your HA `config/custom_components/`
directory and restart Home Assistant.

## Configuration

The config flow asks for two values:

| Field | Description | Example |
| --- | --- | --- |
| **URL** | The `/converse` endpoint of your external agent service | `http://192.168.1.50:8088/converse` |
| **Secret** | A shared secret; sent as `Authorization: Bearer <secret>` and validated by the service | (any strong random string) |

Once configured, assign the agent under **Settings → Voice Assistants →
(your pipeline) → Conversation agent → Claude Bridge**.

## Requirements

- Home Assistant 2025.2.0 or newer.
- The external agent service (`service/`) running and reachable from HA.
- The [Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
  integration enabled in HA if you want the agent to control devices.

## License

See repository for license details.
