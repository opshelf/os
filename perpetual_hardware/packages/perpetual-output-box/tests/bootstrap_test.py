from perpetual_output_box.utils import get_usb_serial


if __name__ == '__main__':
    devices = get_usb_serial()
    for device in devices:
        print(device)
