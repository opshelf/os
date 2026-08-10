from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock
from typing import AsyncIterator, Callable, Hashable
from weakref import ReferenceType, WeakKeyDictionary, ref


@dataclass(frozen=True)
class LoginBackoffDecision:
	retry_after_seconds: int


@dataclass
class _LoginFailureState:
	failure_count: int
	retry_at: float


@dataclass
class _AttemptLocks:
	stripes: tuple[asyncio.Lock, ...]
	overflow: asyncio.Lock


class LoginBackoff:
	"""Enforce a bounded exponential cooldown after failed PIN attempts."""
	_ATTEMPT_LOCK_COUNT = 64
	_DEFAULT_MAX_ENTRIES = 10_000
	_DEFAULT_HISTORY_TTL_SECONDS = 300

	def __init__(
		self,
		*,
		base_delay_seconds: int = 1,
		max_delay_seconds: int = 60,
		max_entries: int = _DEFAULT_MAX_ENTRIES,
		history_ttl_seconds: int = _DEFAULT_HISTORY_TTL_SECONDS,
		clock: Callable[[], float] = time.monotonic,
	) -> None:
		if max_entries < 1:
			raise ValueError("max_entries must be positive")
		if history_ttl_seconds < 0:
			raise ValueError("history_ttl_seconds cannot be negative")

		self._base_delay_seconds = base_delay_seconds
		self._max_delay_seconds = max_delay_seconds
		self._max_entries = max_entries
		self._history_ttl_seconds = history_ttl_seconds
		self._clock = clock
		self._failures: dict[Hashable, _LoginFailureState] = {}
		self._overflow_failure: _LoginFailureState | None = None
		self._reserved_keys: dict[Hashable, int] = {}
		self._lock = Lock()
		# Contended asyncio locks retain their bound loop, so keep values weak too.
		self._attempt_locks_by_loop: WeakKeyDictionary[
			asyncio.AbstractEventLoop, ReferenceType[_AttemptLocks]
		] = WeakKeyDictionary()
		self._attempt_uses_overflow: ContextVar[bool | None] = ContextVar(
			"login_backoff_attempt_uses_overflow",
			default=None,
		)

	def _keyed_entry_count(self) -> int:
		return len(self._failures) + sum(
			key not in self._failures for key in self._reserved_keys
		)

	def _attempt_locks_for_current_loop(self) -> _AttemptLocks:
		loop = asyncio.get_running_loop()
		attempt_locks_ref = self._attempt_locks_by_loop.get(loop)
		attempt_locks = (
			attempt_locks_ref() if attempt_locks_ref is not None else None
		)
		if attempt_locks is None:
			attempt_locks = _AttemptLocks(
				stripes=tuple(
					asyncio.Lock() for _ in range(self._ATTEMPT_LOCK_COUNT)
				),
				overflow=asyncio.Lock(),
			)
			self._attempt_locks_by_loop[loop] = ref(attempt_locks)
		return attempt_locks

	@asynccontextmanager
	async def attempt(self, key: Hashable) -> AsyncIterator[None]:
		"""Serialize complete attempts using bounded per-key and overflow locks."""
		reserved = False
		with self._lock:
			now = self._clock()
			self._prune_expired(now)
			uses_overflow = (
				key not in self._failures
				and key not in self._reserved_keys
				and (
					self._overflow_failure is not None
					or self._keyed_entry_count() >= self._max_entries
				)
			)
			if not uses_overflow:
				self._reserved_keys[key] = self._reserved_keys.get(key, 0) + 1
				reserved = True
			attempt_locks = self._attempt_locks_for_current_loop()

		lock = (
			attempt_locks.overflow
			if uses_overflow
			else attempt_locks.stripes[hash(key) % len(attempt_locks.stripes)]
		)
		try:
			async with lock:
				token = self._attempt_uses_overflow.set(uses_overflow)
				try:
					yield
				finally:
					self._attempt_uses_overflow.reset(token)
		finally:
			if reserved:
				with self._lock:
					if reservation_count := self._reserved_keys.get(key):
						if reservation_count == 1:
							del self._reserved_keys[key]
						else:
							self._reserved_keys[key] = reservation_count - 1

	def _prune_expired(self, now: float) -> None:
		for key, state in tuple(self._failures.items()):
			if state.retry_at + self._history_ttl_seconds <= now:
				del self._failures[key]
		if (
			self._overflow_failure is not None
			and self._overflow_failure.retry_at + self._history_ttl_seconds <= now
		):
			self._overflow_failure = None

	async def check(self, key: Hashable) -> LoginBackoffDecision | None:
		with self._lock:
			now = self._clock()
			self._prune_expired(now)
			state = self._failures.get(key) or self._overflow_failure
			if state is None:
				return None

			remaining = state.retry_at - now
			if remaining <= 0:
				return None

			return LoginBackoffDecision(retry_after_seconds=max(1, int(remaining + 0.999)))

	async def record_failure(self, key: Hashable) -> LoginBackoffDecision:
		with self._lock:
			now = self._clock()
			self._prune_expired(now)
			previous = self._failures.get(key)
			uses_overflow = self._attempt_uses_overflow.get()
			is_overflow = previous is None and (
				uses_overflow is True
				or (
					uses_overflow is None
					and (
						self._keyed_entry_count() >= self._max_entries
						or self._overflow_failure is not None
					)
				)
			)
			if is_overflow:
				previous = self._overflow_failure

			failure_count = 1 if previous is None else previous.failure_count + 1
			delay = min(
				self._max_delay_seconds,
				self._base_delay_seconds * (2 ** (failure_count - 1)),
			)
			state = _LoginFailureState(
				failure_count=failure_count,
				retry_at=now + delay,
			)
			if is_overflow:
				self._overflow_failure = state
			else:
				self._failures[key] = state
			return LoginBackoffDecision(retry_after_seconds=delay)

	async def record_success(self, key: Hashable) -> None:
		with self._lock:
			now = self._clock()
			self._prune_expired(now)
			if self._attempt_uses_overflow.get() is True:
				previous = self._overflow_failure
				failure_count = 1 if previous is None else previous.failure_count
				delay = min(
					self._max_delay_seconds,
					self._base_delay_seconds * (2 ** (failure_count - 1)),
				)
				self._overflow_failure = _LoginFailureState(
					failure_count=failure_count,
					retry_at=now + delay,
				)
			elif key in self._failures:
				self._failures[key] = _LoginFailureState(
					failure_count=0,
					retry_at=now,
				)

	async def clear(self) -> None:
		with self._lock:
			self._failures.clear()
			self._overflow_failure = None
			self._reserved_keys.clear()


login_backoff = LoginBackoff()
