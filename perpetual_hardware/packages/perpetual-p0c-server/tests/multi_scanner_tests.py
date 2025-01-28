from perpetual_p0c_server.core.scanner.waveshare import WaveshareScanner
from perpetual_p0c_server.core.scanner.utils import get_usb_serial
import re
import subprocess
device_re = re.compile(b"Bus\s+(?P<bus>\d+)\s+Device\s+(?P<device>\d+).+ID\s(?P<id>\w+:\w+)\s(?P<tag>.+)$", re.I)
df = subprocess.check_output("lsusb")
devices = []
for i in df.split(b'\n'):
    if i:
        info = device_re.match(i)
        if info:
            dinfo = info.groupdict()
            dinfo['device'] = '/dev/bus/usb/%s/%s' % (dinfo.pop('bus'), dinfo.pop('device'))
            devices.append(dinfo)

for device in devices:
    print(device)



if __name__ == '__main__':
    usb_serial = get_usb_serial()
    for ser in usb_serial:
        print(ser)

