from perpetual_label_maker import LabelMaker, LabelInformation, LabelPrintPayload
import datetime

from perpetual_label_maker.base import ThermalPrinter

today = datetime.date.today()
formatted_date = today.strftime("%m/%d/%Y")


"""
class LabelInformation(BaseModel):
    patient_name: str = "Doe, Jane"
    medication_name: str = "METFORMIN HCL"
    medication_strength: str = "500 MG"
    medication_form: str = "TABLET"
    generic_equivalent: str = "Brand Name"
    instructions: str = "Take 1 tablet orally\n2 times a day"
    pharmacy_name: str = "Pharmacy"
    pharmacy_address: str = "555 Main St\nNew York NY 10038"
    prescriber_name: str = "Dr. John Smith"
    date_filled: str = "MM/DD/YYYY"
    rph_name: str = "Sarah Williams"
    manufacturer_name: str = "Manufacturer"

"""
"""
Allopurinol Tablets, USP		
30
Jeremiah Clarke	

Metformin Hydrochloride Extended-Release Tablets, USP		
30				
Zoe Russell

Levothyroxine Sodium Tablets USP		
30	
Judith Stafford

buPROPion HCl Extended-Release Tabelts, USP (XL)		
30				
Catrin Andrews
"""


label_1 = LabelInformation(
    patient_name="Jeremiah Clarke",
    medication_name="Allopurinol Tablets, USP",
    medication_strength="",
    medication_form="",
    generic_equivalent="",
    instructions="",
    pharmacy_name="",
    date_filled=formatted_date,
    manufacturer_name="",
)

label_2 = LabelInformation(
    patient_name="Zoe Russell",
    medication_name="Metformin Hydrochloride Extended-Release Tablets, USP",
    medication_strength="",
    medication_form="",
    generic_equivalent="",
    instructions="",
    pharmacy_name="",
    date_filled=formatted_date,
    manufacturer_name="",
)

label_3 = LabelInformation(
    patient_name="Judith Stafford",
    medication_name="Levothyroxine Sodium Tablets USP",
    medication_strength="",
    medication_form="",
    generic_equivalent="",
    instructions="",
    pharmacy_name="",
    date_filled=formatted_date,
    manufacturer_name="",
)

label_4 = LabelInformation(
    patient_name="Catrin Andrews",
    medication_name="buPROPion HCl Extended-Release Tabelts, USP (XL)",
    medication_strength="",
    medication_form="",
    generic_equivalent="",
    instructions="",
    pharmacy_name="",
    date_filled=formatted_date,
    manufacturer_name="",
)

def get_gs1(serial_number: str) -> str:
    GTIN = "0" * 14
    EXPIRATION_DATE = "250228"
    LOT = "123456789"
    return "21" + serial_number + chr(29) + "01" + GTIN + "10" + LOT + chr(29) + "17" + EXPIRATION_DATE 


if __name__ == '__main__':
    label_maker = LabelMaker()
    printer = ThermalPrinter()
    """
    label_maker.create_label(
        LabelPrintPayload(
            gs1=get_gs1("0"),
            order_id="0",
            quantity=30
        ),
        label_1
    )
    label_maker.create_label(
        LabelPrintPayload(
            gs1=get_gs1("1"),
            order_id="1",
            quantity=30
        ),
        label_2
    )
    label_maker.create_label(
        LabelPrintPayload(
            gs1=get_gs1("2"),
            order_id="2",
            quantity=30
        ),
        label_3
    )
    label_maker.create_label(
        LabelPrintPayload(
            gs1=get_gs1("3"),
            order_id="3",
            quantity=30
        ),
        label_4
    )
    """
    printer.print(
        LabelPrintPayload(
            gs1=get_gs1("0"),
            order_id="0",
            quantity=30
        ),
        label_1
    )
    printer.print(
        LabelPrintPayload(
            gs1=get_gs1("1"),
            order_id="1",
            quantity=30
        ),
        label_2
    )
    printer.print(
        LabelPrintPayload(
            gs1=get_gs1("2"),
            order_id="2",
            quantity=30
        ),
        label_3
    )
    printer.print(
        LabelPrintPayload(
            gs1=get_gs1("3"),
            order_id="3",
            quantity=30
        ),
        label_4
    )
