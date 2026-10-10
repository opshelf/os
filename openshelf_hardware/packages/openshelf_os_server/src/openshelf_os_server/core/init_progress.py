import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

PENDING = "pending"
CONNECTING = "connecting"
CONFIGURING = "configuring"
HOMING = "homing"
READY = "ready"
FAILED = "failed"

OVERALL_INITIALIZING = "initializing"
OVERALL_READY = "ready"
OVERALL_DEGRADED = "degraded"
OVERALL_FAILED = "failed"


class RobotInitTracker:
    """Holds per-component hardware bring-up state and broadcasts it to SSE subscribers.

    The tracker is the single source of truth for initialization progress. It is
    populated by the background ``init_robot_worker`` task as each component is
    connected, configured, and homed. It stays valid even when the robot worker
    is ``None`` (mid-init or after a fatal failure), so the UI always has data.
    """

    def __init__(self) -> None:
        self._components: dict[str, dict[str, object]] = {}
        self._order: list[str] = []
        self._degrading: set[str] = set()
        self._overall: str = OVERALL_INITIALIZING
        self._error: Optional[str] = None
        self._subscribers: list[asyncio.Queue[str]] = []

    def reset(self) -> None:
        self._components = {}
        self._order = []
        self._degrading = set()
        self._overall = OVERALL_INITIALIZING
        self._error = None
        self._broadcast()

    def register_components(self, names: list[str], *, degrade: bool = True) -> None:
        for name in names:
            if name not in self._components:
                self._components[name] = {
                    "component": name,
                    "phase": PENDING,
                    "detail": "",
                    "reason": None,
                    "updated_at": time.time(),
                }
                self._order.append(name)
            if degrade is True:
                self._degrading.add(name)
        self._broadcast()

    def report(
        self,
        component: str,
        phase: str,
        detail: str = "",
        reason: Optional[str] = None,
    ) -> None:
        entry = self._components.get(component)
        if entry is None:
            entry = {"component": component}
            self._components[component] = entry
            self._order.append(component)
            self._degrading.add(component)

        entry["phase"] = phase
        entry["detail"] = detail or ""
        entry["reason"] = reason
        entry["updated_at"] = time.time()

        logger.info(
            "init_progress",
            extra={"component": component, "phase": phase, "reason": reason},
        )
        if self._overall == OVERALL_READY and phase == FAILED and component in self._degrading:
            self._overall = OVERALL_DEGRADED
            self._error = reason
        elif self._overall == OVERALL_DEGRADED and self._all_components_ready():
            self._overall = OVERALL_READY
            self._error = None
        self._broadcast()

    def set_overall(self, phase: str, error: Optional[str] = None) -> None:
        self._overall = phase
        self._error = error
        self._broadcast()

    def failed_components(self, *, degrading_only: bool = False) -> list[str]:
        if degrading_only is True:
            return [
                name
                for name in self._order
                if name in self._degrading and self._components[name].get("phase") == FAILED
            ]
        return [name for name in self._order if self._components[name].get("phase") == FAILED]

    def failure_reasons(self, names: list[str]) -> dict[str, str]:
        return {
            name: str(self._components[name].get("reason") or "")
            for name in names
            if name in self._components
        }

    def _all_components_ready(self) -> bool:
        return all(
            self._components[name].get("phase") == READY
            for name in self._order
            if name in self._degrading
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "phase": self._overall,
            "error": self._error,
            "components": [self._components[name] for name in self._order],
        }

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot())

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            return

    def _broadcast(self) -> None:
        payload = self.snapshot_json()
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                continue


_tracker = RobotInitTracker()


def get_init_tracker() -> RobotInitTracker:
    return _tracker
