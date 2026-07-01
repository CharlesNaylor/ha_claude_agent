"""Conversation agent that forwards utterances to the external Claude Bridge service."""
from __future__ import annotations

from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationInput,
    ConversationResult,
    ChatLog,
    AssistantContent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SECRET, CONF_URL


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
            conversation_id=user_input.conversation_id,
            response=response,
            continue_conversation=False,
        )
