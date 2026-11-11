import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

# Backsy names archives "<UTC timestamp>_<target><.tar.gz|.tar.zst>[.gpg]".
_TIMESTAMP = r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})"
_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"


def parse_backup_time(name: str, target: str) -> datetime | None:
    """UTC time encoded in the name of a backup of exactly this target; None for anything else."""
    match = re.fullmatch(_TIMESTAMP + "_" + re.escape(target) + r"\.tar\.(?:gz|zst)(?:\.gpg)?", name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_expired(names: Iterable[str], target: str, keep_last: int, keep_days: int | None,
                   now: datetime, protect: Iterable[str] = ()) -> list[str]:
    """
    Which of `names` (file names, without any folder) may be deleted. Only backups of `target`
    are ever considered; every other name is left alone. A backup is KEPT if it is
      - one of the newest `keep_last`, or
      - younger than `keep_days` days (when set), or
      - in `protect` (e.g. the archive that was just uploaded).
    Everything else is returned, oldest first. Age comes from the timestamp in the name,
    not from the object's modification time.
    """
    backups = []
    for name in set(names):
        stamp = parse_backup_time(name, target)
        if stamp is not None:
            backups.append((stamp, name))
    backups.sort(reverse=True)   # newest first

    keep = {name for _, name in backups[:keep_last]} | set(protect)
    if keep_days is not None:
        cutoff = now - timedelta(days=keep_days)
        keep |= {name for stamp, name in backups if stamp >= cutoff}

    return [name for _, name in reversed(backups) if name not in keep]
