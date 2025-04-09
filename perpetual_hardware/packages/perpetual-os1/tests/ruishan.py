import serial
import time

if __name__ == '__main__':
    scale = serial.Serial("/dev/ttyUSB0", 9600)
    while True:
        if scale.in_waiting:
            print(scale.read(scale.in_waiting))
        time.sleep(1)

