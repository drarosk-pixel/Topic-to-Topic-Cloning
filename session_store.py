"""
Persist userbot StringSessions on disk, keyed by Telegram user id.

NOTE: A StringSession grants FULL access to the logged-in account.
Keep sessions.json private. Do NOT commit it to git.
"""

import json
import os
import threading

_lock = threading.Lock()


def _read(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_all(path: str) -> dict:
    with _lock:
        return _read(path)


def save(path: str, user_id: int, session_string: str) -> None:
    with _lock:
        data = _read(path)
        data[str(user_id)] = session_string
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def remove(path: str, user_id: int) -> None:
    with _lock:
        data = _read(path)
        if str(user_id) in data:
            del data[str(user_id)]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
