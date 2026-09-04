import pytest
from pydantic import ValidationError

from openshelf_os.config.settings import PrinterSettings


def test_printer_feed_dots_defaults_to_tuned_value() -> None:
    assert PrinterSettings().feed_dots == 18


@pytest.mark.parametrize("feed_dots", [-1, 129])
def test_printer_feed_dots_rejects_unsafe_values(feed_dots: int) -> None:
    with pytest.raises(ValidationError):
        PrinterSettings(feed_dots=feed_dots)
