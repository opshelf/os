from unittest.mock import patch

from PIL import Image, ImageOps

from openshelf_os_server.core import label_printer


class _SolidBarcode:
    def render(self, *, writer_options: dict[str, object]) -> Image.Image:
        assert writer_options == {
            "module_height": 12.0,
            "quiet_zone": 1,
            "write_text": False,
            "font_size": 0,
            "text_distance": 3,
        }
        return Image.new("L", (100, 50), 0)


def test_render_label_uses_tuned_alignment() -> None:
    with patch.object(label_printer.barcode, "get", return_value=_SolidBarcode()) as get:
        label = label_printer.render_label("036000291452")

    assert label.size == (384, 203)
    assert ImageOps.invert(label).getbbox() == (6, 0, 360, 139)
    get.assert_called_once()
    assert get.call_args.args[:2] == ("code128", "036000291452")


def test_build_label_job_adds_blank_raster_rows_for_feed() -> None:
    job = label_printer.build_label_job("036000291452")

    expected_prefix = b"\x1b@\x1b7\x09\x50\x02\x1dv0\x00\x30\x00\xe1\x00"
    bytes_per_row = 48
    raster_height = 225
    assert job.startswith(expected_prefix)
    assert len(job) == len(expected_prefix) + bytes_per_row * raster_height
    assert job[-bytes_per_row * 22 :] == bytes(bytes_per_row * 22)
