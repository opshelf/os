import pytest
from pydantic import ValidationError

from openshelf_os.config.models import IOBlock, IOBoxSizerConfig, IOBoxSizerToFConfig, Motors


def test_iobox_sizer_defaults_are_observer_safe() -> None:
    config = IOBoxSizerConfig()

    assert config.enabled is False
    assert config.observer_mode_enabled is True
    assert config.measure_on_store is True
    assert config.tof.channels == [6, 7]


def test_ioblock_does_not_require_legacy_usb_camera() -> None:
    block = IOBlock(motors=Motors(root=[]))

    assert block.usb_camera is None
    assert block.sizer.enabled is False


@pytest.mark.parametrize("channels", [[6, 6], [-1], [8], []])
def test_tof_channels_are_validated(channels: list[int]) -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(channels=channels)


@pytest.mark.parametrize("address", [-1, 128])
def test_tof_addresses_are_validated(address: int) -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(mux_address=address)


@pytest.mark.parametrize("samples", [0, 26])
def test_tof_samples_are_validated(samples: int) -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(samples=samples)


def test_tof_aggregation_is_validated() -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(aggregation="mode")


def test_tof_roi_dimensions_must_be_paired() -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(roi_width=4)


@pytest.mark.parametrize("roi_center", [-1, 256])
def test_tof_roi_center_is_validated(roi_center: int) -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(roi_center=roi_center)


def test_tof_preferred_sensor_index_must_reference_channel() -> None:
    with pytest.raises(ValidationError):
        IOBoxSizerToFConfig(channels=[6, 7], preferred_sensor_index=2)
