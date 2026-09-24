"""Commit-scoped cache for the status stream's storage occupancy overlay.

`routers.robot.stream_sse` overlays committed item locations and at-home bins onto
every worker status push. The worker pushes on every state change: command
lifecycle, scale readings, safety inputs and the post-commit refresh in
`core.robot_events`. The occupancy rows, however, only change when a transaction
commits, so reading them per frame issued the same SELECT pair once per frame per
subscriber inside the single long-lived transaction Sentry records for the
stream. That repeated identical query is the pattern behind N+1 issue 7750630568.

Snapshots are therefore keyed to a commit generation maintained from SQLAlchemy's
session `after_commit` event, which fires for every writer in this process:
frames produced between commits reuse the last snapshot, while the first frame
after a commit re-reads, so the overlay still reflects committed state right after
`_refresh_status_after_commit` pushes a fresh snapshot. Invalidating on any commit
is deliberately coarse; a spurious bump costs one extra read, whereas a missed one
would pin stale occupancy for the life of a stream. Every inventory writer in a
deployment is a session in the API process (the single `api` container, including
startup migrations and the demo seed subprocess, which run before the app serves
requests), so a commit is a complete invalidation signal.
"""

from __future__ import annotations

import threading
from typing import Optional, Set, Tuple

from sqlalchemy import event
from sqlalchemy.orm import Session

from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.session import AsyncSessionLocal

OccupiedCellKey = Tuple[str, int, int]
OccupiedCells = Set[OccupiedCellKey]

_generation_lock = threading.Lock()
_commit_generation = 0
_snapshot: Optional[Tuple[int, OccupiedCells]] = None


def commit_generation() -> int:
    """Return the current process-wide commit generation."""
    with _generation_lock:
        return _commit_generation


def invalidate() -> None:
    """Force the next snapshot read to hit the database."""
    global _commit_generation
    with _generation_lock:
        _commit_generation += 1


def reset() -> None:
    """Drop the cached snapshot and restart the commit generation (tests)."""
    global _commit_generation, _snapshot
    with _generation_lock:
        _commit_generation = 0
        _snapshot = None


@event.listens_for(Session, "after_commit")
def _invalidate_on_commit(_session: Session) -> None:
    """Bump the generation for every committed session in this process.

    `AsyncSession` commits through an underlying `Session`, so listening on the
    `Session` class covers the async sessions the app uses as well.
    """
    invalidate()


async def get_occupied_cells() -> OccupiedCells:
    """Return committed occupied cells, re-reading only after a commit.

    The commit generation is read before the query so a commit landing while the
    query runs leaves the stored tag behind and forces one re-read on the next
    call instead of serving a snapshot tagged as current.
    """
    global _snapshot

    generation = commit_generation()
    cached = _snapshot
    if cached is not None and cached[0] == generation:
        return set(cached[1])

    async with AsyncSessionLocal() as session:
        rows = await StorageService(session).get_all_occupied_cells()

    cells: OccupiedCells = {(row[0], row[1], row[2]) for row in rows}
    _snapshot = (generation, cells)
    return set(cells)
