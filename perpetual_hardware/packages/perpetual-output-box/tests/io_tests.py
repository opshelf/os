from perpetual_output_box.rpi_io import RpiIO
import time



if __name__ == '__main__':
    rpi_io = RpiIO()
    state = False
    while True:
        print(
            rpi_io.get_door_sensor(),
            rpi_io.get_break_beam_sensor()
        ) 
        rpi_io.set_door_locked(state)
        state = not state
        time.sleep(1)

