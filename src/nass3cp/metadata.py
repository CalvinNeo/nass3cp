import os
from typing import Optional


def birthtime_ns(details: os.stat_result) -> Optional[int]:
    """Return creation time only when the platform actually exposes it."""
    value = getattr(details, "st_birthtime_ns", None)
    if value is not None:
        return int(value)
    value = getattr(details, "st_birthtime", None)
    if value is not None:
        return int(value * 1_000_000_000)
    # Older Windows Python exposes creation time as ctime. Unix does not.
    return details.st_ctime_ns if os.name == "nt" else None
