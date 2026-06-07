"""Local MCP adapter for the wa-multiproject-bot WhatsApp bot.

Exposes 21 tools (project + admin) over stdio, calling the bot's
HTTP API at $WP_BASE_URL with bearer tokens supplied via
$WP_ADMIN_TOKEN and $WP_PROJECT_TOKEN.
"""

__version__ = "0.1.0"
