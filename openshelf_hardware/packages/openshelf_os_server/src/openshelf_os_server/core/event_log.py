"""Durable, replayable log of the events streamed on `/api/robot/events`.

The log is append-only and ordered by a monotonic `seq` per device. Both robot
runtimes funnel their events through here: hardware events are appended by the
`RobotEventListener` after their side effects commit, mock events by the mock
runtime before they fan out to subscribers. A committed log row therefore
always precedes client visibility, which is what makes `Last-Event-ID`
resumption and `/api/robot/events/log` replay gap-free.
"""

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy import func, select

from openshelf_os_server.database.models import RobotEvent
from openshelf_os_server.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# The append is retried on sequence collisions; the event listeners are
# single-consumer, so a collision means a concurrent writer landed in between.
# A couple of retries ride out the overlap without masking a real failure.
APPEND_ATTEMPTS = 3
APPEND_RETRY_DELAY_S = 0.1


async def append_robot_event(payload: dict[str, Any]) -> Optional[int]:
    """Persist one event and return its `seq`, or None after the final failure.

    Callers keep the stream going on failure; the relay's per-device counter
    detects the resulting gap, so a wedged append must never block the stream.
    """
    for attempt in range(APPEND_ATTEMPTS):
        try:
            async with AsyncSessionLocal() as session:
                seq = await _next_seq(session)
                session.add(
                    RobotEvent(
                        seq=seq,
                        command_id=_extract_command_id(payload),
                        payload=payload,
                    )
                )
                await session.commit()
                return seq
        except Exception as err:
            if attempt + 1 >= APPEND_ATTEMPTS:
                logger.error(
                    "Failed to append robot event after %d attempts: %s",
                    APPEND_ATTEMPTS,
                    err,
                )
                return None
            await asyncio.sleep(APPEND_RETRY_DELAY_S)
    return None


async def read_robot_events(since_seq: int, limit: int) -> list[RobotEvent]:
    """Events with `seq > since_seq`, oldest first, at most `limit` rows."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RobotEvent)
            .where(RobotEvent.seq > since_seq)
            .order_by(RobotEvent.seq)
            .limit(limit)
        )
        return list(result.scalars().all())


async def latest_robot_event_seq() -> int:
    async with AsyncSessionLocal() as session:
        value = await session.scalar(select(func.max(RobotEvent.seq)))
        return int(value or 0)


async def _next_seq(session) -> int:
    value = await session.scalar(
        select(func.coalesce(func.max(RobotEvent.seq), 0) + 1)
    )
    return int(value)


def _extract_command_id(payload: dict[str, Any]) -> Optional[str]:
    command_id = payload.get("command_id")
    return str(command_id) if command_id is not None else None
