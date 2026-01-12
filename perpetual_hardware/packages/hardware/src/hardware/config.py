import json

CONFIG_FILE = './config/config_hardware.json'

class HardwareConfig:
    def __init__(self, default_file):
        self.__config = {}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as file:
                data = json.load(file)
                print(data)
        except FileNotFoundError:
            print(f'Error: {CONFIG_FILE} not found')
        except json.JSONEdcodeError:
            print(f'Error: Could not decode JSON. Check syntax.')


        
if __name__ == "main":
    hardware_config = HardwareConfig(CONFIG_FILE)