import random

def get_random_avatar() -> str:
    avatars = ["newimg.svg", "newimg1.svg", "newimg2.svg", "newimg3.svg", "newimg4.svg"]
    return f"/chat-avatars/{random.choice(avatars)}"

from datetime import datetime, timezone
from typing import Any

def format_timestamp_iso(val: Any) -> str | None:
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc).isoformat()
        return val.isoformat()
    if isinstance(val, str):
        if val.endswith('Z'):
            return val.replace('Z', '+00:00')
        if '+' not in val and '-' not in val[-6:]:
            return val + '+00:00'
        return val
    return str(val)
