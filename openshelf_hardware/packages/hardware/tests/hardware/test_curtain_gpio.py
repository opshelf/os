from __future__ import annotations

import asyncio
import logging

import pytest

from hardware.curtain import CurtainMonitor, CurtainSensor
from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec, GpioSubscription


class SilentSubscriptionInput:
    """Input whose subscription succeeds but never dispatches an edge."""

    pin = 1

    def __init__(self) -> None:
        self.value = False
        self.fail_reads = 0

    def read(self) -> bool:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise OSError("temporary GPIO read failure")
        return self.value

    def subscribe(self, _callback, *, emit_initial: bool = False) -> GpioSubscription:
        del emit_initial
        return GpioSubscription(lambda: None)


@pytest.mark.asyncio
async def test_curtain_monitor_uses_gpio_subscription(caplog: pytest.LogCaptureFixture) -> None:
    manager = GpioManager(
        {
            "light_curtain": GpioPinSpec(
                name="light_curtain",
                board="pca9535",
                expander="pca9535_1",
                pin=1,
                direction="input",
                inverted=True,
            )
        },
        fake=True,
    )
    sensor = CurtainSensor(input_device=manager.input("light_curtain"))
    changes: list[bool] = []
    monitor = CurtainMonitor(sensor, on_change=changes.append)

    with caplog.at_level(logging.INFO, logger="hardware.curtain.curtain"):
        await monitor.start()
        manager.set_fake_input("light_curtain", True)
        await asyncio.sleep(0)
        await monitor.stop()

    assert changes == [False, True]
    assert any(getattr(record, "source", None) == "interrupt" for record in caplog.records)


@pytest.mark.asyncio
async def test_curtain_monitor_polling_recovers_after_transient_read_failure() -> None:
    input_device = SilentSubscriptionInput()
    sensor = CurtainSensor(input_device=input_device)  # type: ignore[arg-type]
    changes: list[bool] = []
    changed = asyncio.Event()

    def on_change(value: bool) -> None:
        changes.append(value)
        if value is True:
            changed.set()

    monitor = CurtainMonitor(sensor, poll_interval=0.001, on_change=on_change)
    await monitor.start()

    try:
        input_device.fail_reads = 1
        input_device.value = True
        await asyncio.wait_for(changed.wait(), timeout=0.1)
    finally:
        await monitor.stop()

    assert changes == [False, True]


@pytest.mark.asyncio
async def test_curtain_monitor_polls_when_subscription_silently_misses_edge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_device = SilentSubscriptionInput()
    sensor = CurtainSensor(input_device=input_device)  # type: ignore[arg-type]
    changes: list[bool] = []
    changed = asyncio.Event()

    def on_change(value: bool) -> None:
        changes.append(value)
        if value is True:
            changed.set()

    monitor = CurtainMonitor(sensor, poll_interval=0.001, on_change=on_change)
    with caplog.at_level(logging.INFO, logger="hardware.curtain.curtain"):
        await monitor.start()

        try:
            input_device.value = True
            await asyncio.wait_for(changed.wait(), timeout=0.1)
        finally:
            await monitor.stop()

    assert changes == [False, True]
    assert any(getattr(record, "source", None) == "poll" for record in caplog.records)
