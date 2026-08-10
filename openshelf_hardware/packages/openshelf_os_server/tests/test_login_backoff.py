import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from openshelf_os_server.core.login_backoff import LoginBackoff, LoginBackoffDecision
from openshelf_os_server.main import app


def test_login_backoff_reuses_singleton_state_across_event_loops() -> None:
	now = [100.0]
	backoff = LoginBackoff(clock=lambda: now[0])
	key = ("client", "user@example.com")

	async def run_wave() -> list[LoginBackoffDecision | None]:
		start = asyncio.Event()

		async def attempt() -> LoginBackoffDecision | None:
			await start.wait()
			async with backoff.attempt(key):
				decision = await backoff.check(key)
				await asyncio.sleep(0)
				if decision is None:
					await backoff.record_failure(key)
				return decision

		tasks = [asyncio.create_task(attempt()) for _ in range(2)]
		await asyncio.sleep(0)
		start.set()
		return await asyncio.gather(*tasks)

	assert asyncio.run(run_wave()).count(None) == 1
	assert asyncio.run(run_wave()) == [
		LoginBackoffDecision(retry_after_seconds=1),
		LoginBackoffDecision(retry_after_seconds=1),
	]


@pytest.mark.anyio
async def test_login_backoff_grows_exponentially_and_resets() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		base_delay_seconds=1,
		max_delay_seconds=8,
		clock=lambda: now[0],
	)
	key = ("client", "user@example.com")

	assert (await backoff.record_failure(key)).retry_after_seconds == 1
	now[0] += 1
	assert (await backoff.record_failure(key)).retry_after_seconds == 2
	now[0] += 2
	assert (await backoff.record_failure(key)).retry_after_seconds == 4
	now[0] += 4
	assert (await backoff.record_failure(key)).retry_after_seconds == 8
	now[0] += 8
	assert (await backoff.record_failure(key)).retry_after_seconds == 8

	await backoff.record_success(key)
	assert await backoff.check(key) is None
	assert (await backoff.record_failure(key)).retry_after_seconds == 1


@pytest.mark.anyio
async def test_login_backoff_expires_quiet_failure_history() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		history_ttl_seconds=5,
	)
	key = ("client", "unknown@example.com")

	await backoff.record_failure(key)
	now[0] += 6

	assert await backoff.check(key) is None
	assert backoff._failures == {}
	assert (await backoff.record_failure(key)).retry_after_seconds == 1


@pytest.mark.anyio
async def test_login_backoff_saturation_preserves_existing_histories() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=3,
		history_ttl_seconds=60,
	)
	target = ("client", "target@example.com")
	first = ("client", "first@example.com")
	second = ("client", "second@example.com")
	unknown = ("client", "unknown@example.com")

	assert (await backoff.record_failure(target)).retry_after_seconds == 1
	now[0] += 1
	assert (await backoff.record_failure(target)).retry_after_seconds == 2
	await backoff.record_failure(first)
	await backoff.record_failure(second)

	assert len(backoff._failures) == 3
	assert set(backoff._failures) == {target, first, second}
	assert (await backoff.record_failure(unknown)).retry_after_seconds == 1
	assert (await backoff.check(("client", "another@example.com"))) == (
		LoginBackoffDecision(retry_after_seconds=1)
	)
	assert len(backoff._failures) == 3
	assert set(backoff._failures) == {target, first, second}

	now[0] += 1
	assert (await backoff.record_failure(("client", "another@example.com"))).retry_after_seconds == 2
	now[0] += 1
	assert (await backoff.record_failure(target)).retry_after_seconds == 4

	await backoff.record_success(("client", "succeeded@example.com"))

	assert len(backoff._failures) == 3
	assert set(backoff._failures) == {target, first, second}


@pytest.mark.anyio
async def test_untracked_success_does_not_reset_shared_overflow_cooldown() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=1,
		history_ttl_seconds=60,
	)
	tracked = ("client", "tracked@example.com")
	overflow_failure = ("client", "overflow-failure@example.com")
	overflow_success = ("client", "overflow-success@example.com")
	other_untracked = ("client", "other-untracked@example.com")

	await backoff.record_failure(tracked)
	assert (await backoff.record_failure(overflow_failure)).retry_after_seconds == 1

	await backoff.record_success(overflow_success)

	assert await backoff.check(other_untracked) == LoginBackoffDecision(
		retry_after_seconds=1
	)
	assert (await backoff.record_failure(other_untracked)).retry_after_seconds == 2


@pytest.mark.anyio
async def test_tracked_success_stays_isolated_from_overflow_cooldown() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=1,
		history_ttl_seconds=60,
	)
	tracked = ("client", "tracked@example.com")
	overflow = ("client", "overflow@example.com")

	await backoff.record_failure(tracked)
	await backoff.record_success(tracked)
	assert (await backoff.record_failure(overflow)).retry_after_seconds == 1

	assert await backoff.check(tracked) is None
	assert (await backoff.record_failure(tracked)).retry_after_seconds == 1


@pytest.mark.anyio
async def test_distinct_overflow_attempts_share_expired_cooldown_admission() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=1,
		history_ttl_seconds=60,
	)
	tracked = ("client", "tracked@example.com")
	overflow_keys = [
		("client", "overflow-one@example.com"),
		("client", "overflow-two@example.com"),
		("client", "overflow-three@example.com"),
	]

	await backoff.record_failure(tracked)
	await backoff.record_failure(overflow_keys[0])
	now[0] += 1
	start = asyncio.Event()
	allowed: list[tuple[str, str]] = []
	blocked: list[tuple[str, str]] = []

	async def attempt_overflow(key: tuple[str, str]) -> None:
		await start.wait()
		async with backoff.attempt(key):
			if await backoff.check(key) is None:
				allowed.append(key)
				await asyncio.sleep(0)
				await backoff.record_failure(key)
			else:
				blocked.append(key)

	tasks = [asyncio.create_task(attempt_overflow(key)) for key in overflow_keys]
	await asyncio.sleep(0)
	start.set()
	await asyncio.gather(*tasks)

	assert len(allowed) == 1
	assert len(blocked) == 2


@pytest.mark.anyio
async def test_successful_overflow_attempt_rearms_shared_cooldown() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=1,
		history_ttl_seconds=60,
	)
	tracked = ("client", "tracked@example.com")
	overflow_keys = [
		("client", "overflow-one@example.com"),
		("client", "overflow-two@example.com"),
		("client", "overflow-three@example.com"),
	]

	await backoff.record_failure(tracked)
	await backoff.record_failure(overflow_keys[0])
	now[0] += 1
	assert (await backoff.record_failure(overflow_keys[0])).retry_after_seconds == 2
	now[0] += 2

	async def run_successful_overflow_attempts() -> tuple[int, int]:
		start = asyncio.Event()
		allowed: list[tuple[str, str]] = []
		blocked: list[tuple[str, str]] = []

		async def attempt_overflow(key: tuple[str, str]) -> None:
			await start.wait()
			async with backoff.attempt(key):
				if await backoff.check(key) is None:
					allowed.append(key)
					await asyncio.sleep(0)
					await backoff.record_success(key)
				else:
					blocked.append(key)

		tasks = [asyncio.create_task(attempt_overflow(key)) for key in overflow_keys]
		await asyncio.sleep(0)
		start.set()
		await asyncio.gather(*tasks)
		return len(allowed), len(blocked)

	assert await run_successful_overflow_attempts() == (1, 2)
	assert backoff._overflow_failure is not None
	assert backoff._overflow_failure.failure_count == 2
	assert backoff._overflow_failure.retry_at == now[0] + 2

	now[0] += 2
	assert await run_successful_overflow_attempts() == (1, 2)
	assert backoff._overflow_failure is not None
	assert backoff._overflow_failure.failure_count == 2
	assert backoff._overflow_failure.retry_at == now[0] + 2


@pytest.mark.anyio
async def test_capacity_boundary_reserves_keyed_admission_before_overflow() -> None:
	backoff = LoginBackoff(max_entries=1)
	first = ("client", "first@example.com")
	second = ("client", "second@example.com")
	first_admitted = asyncio.Event()
	release_first = asyncio.Event()
	second_recorded = asyncio.Event()

	async def first_attempt() -> None:
		async with backoff.attempt(first):
			first_admitted.set()
			await release_first.wait()
			await backoff.record_failure(first)

	async def second_attempt() -> None:
		await first_admitted.wait()
		async with backoff.attempt(second):
			await backoff.record_failure(second)
			second_recorded.set()

	first_task = asyncio.create_task(first_attempt())
	second_task = asyncio.create_task(second_attempt())
	await second_recorded.wait()

	assert backoff._failures == {}
	assert backoff._overflow_failure is not None

	release_first.set()
	await asyncio.gather(first_task, second_task)

	assert set(backoff._failures) == {first}
	assert backoff._overflow_failure is not None


@pytest.mark.anyio
async def test_expiring_keyed_attempt_keeps_its_capacity_reservation() -> None:
	now = [100.0]
	backoff = LoginBackoff(
		clock=lambda: now[0],
		max_entries=1,
		history_ttl_seconds=5,
	)
	first = ("client", "first@example.com")
	second = ("client", "second@example.com")
	first_admitted = asyncio.Event()
	release_first = asyncio.Event()

	await backoff.record_failure(first)

	async def first_attempt() -> None:
		async with backoff.attempt(first):
			first_admitted.set()
			await release_first.wait()
			await backoff.record_failure(first)

	first_task = asyncio.create_task(first_attempt())
	await first_admitted.wait()
	assert backoff._reserved_keys == {first: 1}

	now[0] += 6
	async with backoff.attempt(second):
		await backoff.record_failure(second)

	assert backoff._failures == {}
	assert backoff._overflow_failure is not None

	release_first.set()
	await first_task

	assert set(backoff._failures) == {first}
	assert len(backoff._failures) == 1
	assert backoff._overflow_failure is not None
	assert backoff._reserved_keys == {}


@pytest.mark.anyio
async def test_cancelled_waiting_attempt_releases_keyed_reservation() -> None:
	backoff = LoginBackoff(max_entries=2)
	key = ("client", "held@example.com")
	other_key = ("client", "other@example.com")
	release_holder = asyncio.Event()

	async def hold_attempt() -> None:
		async with backoff.attempt(key):
			await release_holder.wait()

	async def waiting_attempt() -> None:
		async with backoff.attempt(key):
			pytest.fail("cancelled attempt acquired the lock")

	holder = asyncio.create_task(hold_attempt())
	await asyncio.sleep(0)
	waiter = asyncio.create_task(waiting_attempt())
	await asyncio.sleep(0)
	assert backoff._reserved_keys == {key: 2}

	waiter.cancel()
	with pytest.raises(asyncio.CancelledError):
		await waiter
	assert backoff._reserved_keys == {key: 1}

	async with backoff.attempt(other_key):
		await backoff.record_failure(other_key)
	assert set(backoff._failures) == {other_key}
	assert backoff._overflow_failure is None

	release_holder.set()
	await holder
	assert backoff._reserved_keys == {}


@pytest.mark.anyio
async def test_wrong_pin_reports_and_enforces_retry_delay(
	unauthenticated_client: AsyncClient,
) -> None:
	first = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "9999"},
	)

	assert first.status_code == 401
	assert first.headers["retry-after"] == "1"
	assert first.json()["detail"] == {
		"message": "Invalid email or PIN",
		"code": "invalid_credentials",
		"retry_after_seconds": 1,
	}

	second = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "9999"},
	)

	assert second.status_code == 429
	assert second.headers["retry-after"] == "1"
	assert second.json()["detail"] == {
		"message": "Too many failed login attempts",
		"code": "login_backoff",
		"retry_after_seconds": 1,
	}


@pytest.mark.anyio
async def test_wrong_pin_backoff_is_shared_across_client_hosts(
	seeded_db: object,
) -> None:
	async with AsyncClient(
		transport=ASGITransport(app=app, client=("198.51.100.10", 8000)),
		base_url="http://test",
	) as first_client:
		first = await first_client.post(
			"/api/auth/login/json",
			json={"email": "admin@opshelf.com", "pin": "9999"},
		)

	async with AsyncClient(
		transport=ASGITransport(app=app, client=("203.0.113.20", 8000)),
		base_url="http://test",
	) as second_client:
		second = await second_client.post(
			"/api/auth/login/json",
			json={"email": "admin@opshelf.com", "pin": "9999"},
		)

	assert first.status_code == 401
	assert second.status_code == 429


@pytest.mark.anyio
async def test_concurrent_wrong_pins_allow_only_one_attempt_through_cooldown(
	unauthenticated_client: AsyncClient,
) -> None:
	start = asyncio.Event()

	async def wrong_pin() -> int:
		await start.wait()
		response = await unauthenticated_client.post(
			"/api/auth/login/json",
			json={"email": "admin@opshelf.com", "pin": "9999"},
		)
		return response.status_code

	requests = [asyncio.create_task(wrong_pin()) for _ in range(4)]
	await asyncio.sleep(0)
	start.set()
	status_codes = await asyncio.gather(*requests)

	assert status_codes.count(401) == 1
	assert status_codes.count(429) == 3
