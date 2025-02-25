from perpetual_p0c_server.core.scanner.utils import get_usb_serial


if __name__ == '__main__':
    ports = get_usb_serial()
    for port in ports:
        print(port)

