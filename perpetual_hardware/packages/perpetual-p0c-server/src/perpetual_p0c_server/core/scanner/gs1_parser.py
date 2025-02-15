from perpetual_p0c.storage import GS1Data

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
                code = raw_string[idx : idx + 2]
                idx += 2
                # print(code)
                if code == "01":
                    parsed_data["gtin"] = raw_string[idx : idx + 14]
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
                    parsed_data["expiry"] = raw_string[idx : idx + 6]
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
                code = raw_string[idx : idx + 2]
                idx += 2
                # print("Not 29", code)
                if code == "01":
                    parsed_data["gtin"] = raw_string[idx : idx + 14]
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
                    parsed_data["expiry"] = raw_string[idx : idx + 6]
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
        return GS1Data(**parsed_data)
