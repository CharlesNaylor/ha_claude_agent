"""Conversation agent that forwards utterances to the external Claude Bridge service.

The external service may reply in one of two ways:

* **Non-streaming** — a single JSON object ``{"reply": "..."}`` with
  ``Content-Type: application/json``. Simplest; the whole answer arrives at once.
* **Streaming** — newline-delimited JSON (NDJSON, ``Content-Type:
  application/x-ndjson``) where each line is one event::

      {"type": "delta", "text": "Turning on "}
      {"type": "delta", "text": "the lights."}
      {"type": "error", "message": "..."}     # optional, mid-stream failure
      {"type": "done"}                          # optional, terminates the stream

  Streaming lets Home Assistant push tokens into the voice pipeline as they
  arrive, so streaming TTS can start speaking before the agent has finished.
"""
from __future__ import annotations

import json

import aiohttp

from homeassistant.components.conversation import (
    AssistantContent,
    ChatLog,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SECRET, CONF_URL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Claude Bridge conversation entity from a config entry."""
    async_add_entities([ClaudeBridgeAgent(entry)])


class ClaudeBridgeAgent(ConversationEntity):
    """A conversation agent that forwards each utterance to the external service."""

    _attr_has_entity_name = True
    _attr_name = "Claude Bridge"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the agent from its config entry."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | str:
        """Return the list of supported languages."""
        return "*"

    async def _async_handle_message(
        self, user_input: ConversationInput, chat_log: ChatLog
    ) -> ConversationResult:
        """Forward the utterance to the external service and speak the reply."""
        try:
            await self._async_forward(user_input, chat_log)
        except Exception as err:  # noqa: BLE001
            chat_log.async_add_assistant_content_without_tools(
                AssistantContent(
                    agent_id=user_input.agent_id,
                    content=f"Sorry, the assistant service failed: {err}",
                )
            )

        last = chat_log.content[-1]
        speech = last.content if isinstance(last, AssistantContent) else None
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech or "")
        return ConversationResult(
            conversation_id=user_input.conversation_id,
            response=response,
            continue_conversation=False,
        )

    async def _async_forward(
        self, user_input: ConversationInput, chat_log: ChatLog
    ) -> None:
        """POST the utterance and feed the reply (streamed or not) into the chat log."""
        session = async_get_clientsession(self.hass)
        payload = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "language": user_input.language,
        }
        headers = {
            "Authorization": f"Bearer {self.entry.data[CONF_SECRET]}",
            "Accept": "application/x-ndjson, application/json",
        }
        # No overall total timeout: a streaming reply can legitimately run long.
        # Cap connect and per-chunk read so a stalled service still fails fast.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)

        async with session.post(
            self.entry.data[CONF_URL], json=payload, headers=headers, timeout=timeout
        ) as resp:
            resp.raise_for_status()

            if resp.headers.get("Content-Type", "").startswith("application/json"):
                # Non-streaming service: one JSON object with the whole reply.
                data = await resp.json()
                chat_log.async_add_assistant_content_without_tools(
                    AssistantContent(
                        agent_id=user_input.agent_id, content=data["reply"]
                    )
                )
                return

            # Streaming service: feed NDJSON deltas into the chat log so the
            # voice pipeline can start speaking before the answer is complete.
            async def _deltas():
                yield {"role": "assistant"}
                async for raw in resp.content:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    match event.get("type"):
                        case "delta":
                            if text := event.get("text"):
                                yield {"content": text}
                        case "error":
                            message = event.get("message", "unknown error")
                            yield {"content": f" [error: {message}]"}
                        case "done":
                            break

            async for _content in chat_log.async_add_delta_content_stream(
                user_input.agent_id, _deltas()
            ):
                pass
