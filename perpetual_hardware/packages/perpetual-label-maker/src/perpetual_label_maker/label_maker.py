from abc import ABCMeta, abstractmethod
from PIL import Image, ImageDraw, ImageFont
from pylibdmtx.pylibdmtx import encode
from .events import LabelPrintPayload
from pydantic import BaseModel


class FontSetting(BaseModel):
    size: int
    name: str


class LabelSettings(BaseModel):
    output_file: str = "prescription_label.png"
    logo_file: str = "./perpetual-logo-vector.jpg"
    width: int = 800
    height: int = 600
    background_color: str = "white"
    text_color: str = "black"
    font_extra_bold: FontSetting = FontSetting(size=40, name="Lato-Medium.ttf")
    font_bold: FontSetting = FontSetting(size=32, name="Lato-Medium.ttf")
    font_regular: FontSetting = FontSetting(size=24, name="Lato-Medium.ttf")
    # Mac
    # TODO: make this detected by the system
    # font_extra_bold: FontSetting = FontSetting(size=40, name="Arial Unicode.ttf")
    # font_bold: FontSetting = FontSetting(size=32, name="Arial Unicode.ttf")
    # font_regular: FontSetting = FontSetting(size=24, name="Arial Unicode.ttf")


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


class AbstractLabelMaker(metaclass=ABCMeta):
    @property
    @abstractmethod
    def settings(self) -> LabelSettings: ...

    @settings.setter
    @abstractmethod
    def settings(self, new_settings: LabelSettings) -> None: ...

    @abstractmethod
    def create_label(
        self, label_data: LabelPrintPayload, information: LabelInformation = LabelInformation()
    ) -> None: ...


class LabelMaker(AbstractLabelMaker):
    def __init__(self, settings: LabelSettings = LabelSettings()) -> None:
        self._settings = settings

    @property
    def settings(self) -> LabelSettings:
        return self._settings

    @settings.setter
    def settings(self, new_settings: LabelSettings) -> None:
        self._settings = new_settings

    def create_label(
        self, label_data: LabelPrintPayload, information: LabelInformation = LabelInformation()
    ) -> None:
        img = Image.new(
            "RGB", (self.settings.width, self.settings.height), self.settings.background_color
        )
        draw = ImageDraw.Draw(img, "RGBA")
        padding = 20
        section_spacing = 40
        x_left = padding
        y = padding
        # Fonts
        font_extra_bold = ImageFont.truetype(
            self.settings.font_extra_bold.name, self.settings.font_extra_bold.size
        )
        font_bold = ImageFont.truetype(self.settings.font_bold.name, self.settings.font_bold.size)
        font_regular = ImageFont.truetype(
            self.settings.font_regular.name, self.settings.font_regular.size
        )

        # Patient name
        draw.text(
            (x_left, y),
            f"{information.patient_name}",
            fill=self.settings.text_color,
            font=font_bold,
        )

        # Medication details
        draw.text(
            (x_left, 220),
            f"{information.medication_name}",
            fill=self.settings.text_color,
            font=font_extra_bold,
        )
        draw.text(
            (x_left, 270),
            f"{information.medication_strength} {information.medication_form}",
            fill=self.settings.text_color,
            font=font_extra_bold,
        )
        draw.text(
            (x_left, 320),
            f"Generic equivalent of {information.generic_equivalent}",
            fill=self.settings.text_color,
            font=font_regular,
        )

        # Instructions
        draw.text(
            (x_left, 460),
            information.instructions,
            fill=self.settings.text_color,
            font=font_regular,
        )

        # Right section (Pharmacy and prescriber details)
        x_right = self.settings.width // 2 + 140

        # Pharmacy name and address
        draw.text(
            (x_right, y), information.pharmacy_name, fill=self.settings.text_color, font=font_bold
        )
        y += section_spacing
        draw.text(
            (x_right, y),
            information.pharmacy_address,
            fill=self.settings.text_color,
            font=font_regular,
        )
        y += section_spacing * 2

        # Prescriber details
        draw.text((x_right, y), f"Prescriber:", fill=self.settings.text_color, font=font_regular)
        y += section_spacing
        draw.text(
            (x_right, y),
            f"{information.prescriber_name}",
            fill=self.settings.text_color,
            font=font_regular,
        )
        y += section_spacing
        draw.text((x_right, y), f"Date filled:", fill=self.settings.text_color, font=font_regular)
        y += section_spacing
        draw.text(
            (x_right, y),
            f"{information.date_filled}",
            fill=self.settings.text_color,
            font=font_regular,
        )
        y += section_spacing
        draw.text((x_right, y), f"RPH:", fill=self.settings.text_color, font=font_regular)
        y += section_spacing
        draw.text(
            (x_right, y),
            f"{information.rph_name}",
            fill=self.settings.text_color,
            font=font_regular,
        )
        y += section_spacing
        draw.text((x_right, y), f"MFR:", fill=self.settings.text_color, font=font_regular)
        y += section_spacing
        draw.text(
            (x_right, y),
            f"{information.manufacturer_name}",
            fill=self.settings.text_color,
            font=font_regular,
        )

        encoded = encode(label_data.gs1.encode("utf8"))
        dm_img = Image.frombytes("RGB", (encoded.width, encoded.height), encoded.pixels)
        dm_img = dm_img.resize((100, 100))
        img.paste(
            dm_img, (self.settings.width - 120, self.settings.height - 120)
        )  # Bottom-right corner

        logo = Image.open(self.settings.logo_file)
        img.paste(logo, (20, self.settings.height - 65))
        draw.rounded_rectangle(
            (10, 10, self.settings.width - 10, self.settings.height - 20),
            fill=(0, 0, 0, 0),
            outline="black",
            width=2,
            radius=8,
        )

        # Save the image
        img.save(self.settings.output_file)
        print(f"Prescription label saved to {self.settings.output_file}")
