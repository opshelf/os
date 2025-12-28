from perpetual_os1_server.routers import calibration


def test_build_error_detail_includes_needs_home() -> None:
    detail = calibration._build_error_detail(
        "Motor error",
        code="motor_fault",
        needs_home=True,
        reason="Axis stall",
    )

    assert detail["message"] == "Motor error"
    assert detail["code"] == "motor_fault"
    assert detail["needs_home"] is True
    assert detail["reason"] == "Axis stall"


def test_clamp_value_records_warning_on_limit() -> None:
    warnings: list[str] = []
    value = calibration._clamp_value(120.0, 100.0, "X axis", warnings)

    assert value == 100.0
    assert len(warnings) == 1
    assert "X axis" in warnings[0]


def test_clamp_value_no_warning_when_in_range() -> None:
    warnings: list[str] = []
    value = calibration._clamp_value(50.0, 100.0, "X axis", warnings)

    assert value == 50.0
    assert warnings == []
