import hid

# Enumerate all HID devices
devices = hid.enumerate()

for device in devices:
    print(f"Device: {device['product_string']} ({device['vendor_id']}, {device['product_id']})")

## Open a specific device
#device = hid.device()
#device.open(vendor_id, product_id)  # Replace with your device's vendor ID and product ID
#
## Read data from the device
#data = device.read(64)  # Read up to 64 bytes
#
## Process the data
## ...
#
## Close the device
#device.close()
