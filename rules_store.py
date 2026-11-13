"""
Persist per-user transformation rules on disk (rules.json), keyed by
Telegram user id. Same safe-write pattern as session_store.
"""

import json
import os
import threading

_lock = threading.Lock()


def _read(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load(path, user_id):
    with _lock:
        return _read(path).get(str(user_id), {})


def save(path, user_id, rules):
    with _lock:
        data = _read(path)
        data[str(user_id)] = rules
        _write(path, data)


def clear(path, user_id):
    with _lock:
        data = _read(path)
        if str(user_id) in data:
            del data[str(user_id)]
            _write(path, data)
