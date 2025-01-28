from perpetual_output_box.io import AbstractIO


class MockIO(AbstractIO):

    def get_door_sensor(self) -> bool:
        return False

    def get_break_beam_sensor(self) -> bool:
        return False

    def get_lock(self) -> bool:
        return False

    def set_door_locked(self, door_locked: bool) -> None:
        print(f"Door Locked: {door_locked}")
        return None
