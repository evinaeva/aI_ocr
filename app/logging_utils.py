import json
from datetime import datetime, timezone


def log_event(event: str, **fields) -> None:
    payload = {
        "event": event,
        "timestamp_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))
