"""
Configuration loaded from environment variables.

Required env vars:
  API_ID     - from https://my.telegram.org  (integer)
  API_HASH   - from https://my.telegram.org  (string)
  BOT_TOKEN  - from @BotFather                (string)

Optional:
  SLEEP_BETWEEN   - delay (seconds) between forward batches (default 0.6)
  SLEEP_PER_TOPIC - delay (seconds) after each topic created (default 0.5)
  SESSIONS_FILE   - path to store user sessions (default sessions.json)
"""

import os


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Environment variable {name} is not set. "
            f"Dekho README.md me setup steps."
        )
    return val


API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")
BOT_TOKEN = _require("BOT_TOKEN")

SLEEP_BETWEEN = float(os.environ.get("SLEEP_BETWEEN", "0.6"))
SLEEP_PER_TOPIC = float(os.environ.get("SLEEP_PER_TOPIC", "0.5"))
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "sessions.json")
RULES_FILE = os.environ.get("RULES_FILE", "rules.json")
