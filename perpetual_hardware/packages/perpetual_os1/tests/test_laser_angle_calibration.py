from perpetual_os1.laser_angle_calibration import get_default_rotation_center_deg


def test_get_default_rotation_center_deg_uses_front_for_left() -> None:
	assert get_default_rotation_center_deg('left') == 0.0


def test_get_default_rotation_center_deg_uses_back_for_right() -> None:
	assert get_default_rotation_center_deg('right') == 180.0
