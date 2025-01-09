#import serial
from typing import Optional
from perpetual_p0c.storage import GS1Data
import asyncio
from pydantic import ValidationError
import serial_asyncio
#from pydantic import ValidationError
from typing import Optional
from .base import AbstractScanner

class GS1Parser:
    AI_FORMATS = {
        "01": {"name": "gtin", "format": "N14"},  # GTIN
        "10": {"name": "batch_number", "format": "X..20"},  # Batch/Lot Number
        "17": {"name": "expiration_date", "format": "N6"},  # Expiration Date (YYMMDD)
        "21": {"name": "serial_number", "format": "X..20"},  # Serial Number
    }

    @staticmethod
    def parse_gs1_to_model(raw_string: str) -> GS1Data:
        parsed_data = {"raw_text": raw_string}
        idx = 0
        while idx < len(raw_string):
            if ord(raw_string[idx]) == 29:
                idx += 1
                code = raw_string[idx:idx+2]
                idx += 2
                #print(code)
                if code == "01":
                    parsed_data["gtin"] = raw_string[idx:idx+14]
                    idx += 14
                elif code == "10":
                    batch_number = ""
                    while idx < len(raw_string):
                        if ord(raw_string[idx]) == 29:
                            idx += 1
                            break
                        else:
                            batch_number += raw_string[idx]
                            idx += 1
                    parsed_data["lot"] = batch_number
                elif code == "17":
                    parsed_data["expiry"] = raw_string[idx:idx+6]
                    idx += 6
                elif code == "21":
                    serial_number = ""
                    while idx < len(raw_string):
                        if ord(raw_string[idx]) == 29:
                            idx += 1
                            break
                        else:
                            serial_number += raw_string[idx]
                            idx += 1
                    parsed_data["sn"] = serial_number
                else:
                    idx += 1
            else:
                code = raw_string[idx:idx+2]
                idx += 2
                #print("Not 29", code)
                if code == "01":
                    parsed_data["gtin"] = raw_string[idx:idx+14]
                    idx += 14
                elif code == "10":
                    batch_number = ""
                    while idx < len(raw_string):
                        if ord(raw_string[idx]) == 29:
                            idx += 1
                            break
                        else:
                            batch_number += raw_string[idx]
                            idx += 1
                    parsed_data["lot"] = batch_number
                elif code == "17":
                    parsed_data["expiry"] = raw_string[idx:idx+6]
                    idx += 6
                elif code == "21":
                    serial_number = ""
                    while idx < len(raw_string):
                        if ord(raw_string[idx]) == 29:
                            idx += 1
                            break
                        else:
                            serial_number += raw_string[idx]
                            idx += 1
                    parsed_data["sn"] = serial_number
                else:
                    idx += 1
        print(parsed_data)
        return GS1Data(**parsed_data)


class WaveshareScanner(AbstractScanner):
    def __init__(self, port: str, baudrate: int) -> None:
        self.port = port
        self.baudrate = baudrate
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def initialize(self) -> None:
        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=self.port,
            baudrate=self.baudrate
        )

    async def read(self) -> bytes:
        if not self.reader:
            raise ConnectionError("Serial connection is not established. Call connect() first.")
        data = await self.reader.readuntil(b'\r')
        return data

    async def scan(self) -> Optional[GS1Data]:
        try:
            data = await self.read()
            decoded_data = data.decode().strip()
            gs1 = GS1Parser.parse_gs1_to_model(decoded_data)
            return gs1
        except ValidationError as e:
            print(e)
            pass
        except Exception as e:
            await self.shutdown()
        return None

    async def shutdown(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

