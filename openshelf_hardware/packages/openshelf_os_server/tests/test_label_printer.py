from openshelf_os_server.core import label_printer


def test_build_label_job_adds_blank_raster_rows_for_feed() -> None:
    job = label_printer.build_label_job("036000291452", feed_dots=18)

    expected_prefix = b"\x1b@\x1b7\x09\x50\x02\x1dv0\x00\x30\x00\xdd\x00"
    bytes_per_row = 48
    raster_height = 221
    assert job.startswith(expected_prefix)
    assert len(job) == len(expected_prefix) + bytes_per_row * raster_height
    assert job[-bytes_per_row * 18 :] == bytes(bytes_per_row * 18)


