from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True, slots=True)
class JsonlRecord:
    start_offset: int
    end_offset: int
    line: str


def file_identity(path: str | Path) -> str:
    """Return a stable identity for the current file object.

    dev/inode lets the consumer distinguish append/truncate from replacement or
    log rotation without relying on mtime, which changes on every append.
    """
    stat = Path(path).stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def iter_complete_records(
    path: str | Path,
    *,
    offset: int = 0,
) -> Iterator[JsonlRecord]:
    """Yield only newline-terminated JSONL records from byte ``offset``.

    A producer may be in the middle of appending its last line when the reader
    wakes up.  Such a partial line is deliberately not yielded; the caller keeps
    its cursor at ``start_offset`` and sees the complete record on the next pass.
    """
    target = Path(path)
    if offset < 0:
        raise ValueError("offset must be >= 0")

    with target.open("r", encoding="utf-8", newline="") as fh:
        fh.seek(offset)
        while True:
            start = fh.tell()
            line = fh.readline()
            if not line:
                return
            end = fh.tell()
            if not line.endswith("\n"):
                return
            yield JsonlRecord(
                start_offset=start,
                end_offset=end,
                line=line,
            )
