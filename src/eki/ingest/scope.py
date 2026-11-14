"""The scope of one ingest: one expert, one date range."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Scope:
    expert_name: str
    expert_email: str
    since: datetime
    until: datetime
    """Inclusive on both ends. Both are timezone-aware."""
    work_email_domains: frozenset[str]
    mail_folders: tuple[str, ...] = ()

    def contains(self, moment: datetime) -> bool:
        return self.since <= moment <= self.until


def validate_mail_folders(folders: tuple[str, ...], *, required: bool = False) -> tuple[str, ...]:
    """Explicit folder names only; no glob, traversal or private/personal namespace."""
    import re
    from pathlib import PurePosixPath

    if required and not folders:
        raise ValueError("mail ingestion requires an explicit folder selection")
    if len(folders) > 50:
        raise ValueError("too many selected mail folders")
    selected = set()
    for folder in folders:
        parts = folder.split("/")
        if (
            not folder
            or len(folder) > 200
            or PurePosixPath(folder).is_absolute()
            or any(p in ("", ".", "..") for p in parts)
            or any(c in folder for c in "\\*?[]")
            or any(ord(c) < 32 or ord(c) == 127 for c in folder)
            or any(
                set(re.findall(r"[a-z]+", p.casefold())) & {"personal", "private"} for p in parts
            )
        ):
            raise ValueError("mail folder selection is not permitted")
        selected.add(folder)
    return tuple(sorted(selected))
