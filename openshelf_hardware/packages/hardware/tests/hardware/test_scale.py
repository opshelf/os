from unittest.mock import AsyncMock, Mock

import pytest

from hardware.scale import Scale


def _scale(statuses: list[bool], weights: list[float | None]) -> Scale:
    scale = Scale(Mock())
    scale.read_status = AsyncMock(side_effect=[{"stable": s} for s in statuses])
    scale.read_weight = AsyncMock(side_effect=weights)

    return scale


@pytest.mark.asyncio
async def test_steady_zero_ends_the_wait_early_with_no_measurement():
    scale = _scale([True] * 3, [0.0] * 3)

    assert await scale.wait_for_stable_weight(timeout=5.0, poll_interval=0) is None
    assert scale.read_status.await_count == Scale.STABLE_ZERO_CONFIRM_READS


@pytest.mark.asyncio
async def test_stable_positive_weight_is_returned():
    scale = _scale([True], [12.5])

    assert await scale.wait_for_stable_weight(timeout=5.0, poll_interval=0) == 12.5


@pytest.mark.asyncio
async def test_stable_zero_after_instability_still_returns_zero():
    scale = _scale([False, True], [0.0, 0.0])

    assert await scale.wait_for_stable_weight(timeout=5.0, poll_interval=0) == 0.0
