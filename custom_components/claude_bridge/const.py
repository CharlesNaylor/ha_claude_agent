"""Constants for the Claude Bridge conversation agent."""

DOMAIN = "claude_bridge"

# Config-entry keys (the *names* of fields, not secret values). The actual
# service URL and shared secret are entered in the config-flow UI and stored
# by Home Assistant in .storage/core.config_entries, never in this repo.
CONF_URL = "url"
CONF_SECRET = "secret"
