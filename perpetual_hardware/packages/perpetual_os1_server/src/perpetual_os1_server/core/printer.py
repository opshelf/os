import random
import barcode
from barcode.writer import ImageWriter
from pydantic import BaseModel
from typing import List, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFile

def create_barcode_upca(filename: str, upca: str) -> Tuple[str, str]:
    upc = barcode.get("upc", upca, writer=ImageWriter())
    filename = upc.save(filename.split(".")[0])
    return upca, filename


def create_barcode(filename: str) -> Tuple[str, str]:
    upca = generate_random_upca()
    upc = barcode.get("upc", upca, writer=ImageWriter())
    filename = upc.save(filename.split(".")[0])
    return upca, filename


def calculate_upc_check_digit(digits: List[str]) -> int:
    odd_sum = sum(int(digits[i]) for i in range(0, 11, 2))
    even_sum = sum(int(digits[i]) for i in range(1, 11, 2))
    total = odd_sum * 3 + even_sum
    check_digit = (10 - (total % 10)) % 10
    return check_digit


def generate_random_upca() -> str:
    digits = [str(random.randint(0, 9)) for _ in range(11)]
    check_digit = calculate_upc_check_digit(digits)
    upc_code = "".join(digits) + str(check_digit)
    return upc_code


class LabelConfiguration(BaseModel):
    barcode_path: str = "barcode.png"
    logo_path: str = "logo.png"
    output_path: str = "label.png"
    attributes: List[Tuple[str, str]] = [
        ("Name:", "Some Chemical"),
        ("Expiration:", "2025-12-31"),
        ("Received:", "2025-04-04"),
    ]
    upca: Optional[str] = None
    label_size: Tuple[int, int] = (500, 350)

def create_label(configuration: LabelConfiguration) -> str:
    label = Image.new("RGB", configuration.label_size, "white")
    draw = ImageDraw.Draw(label)
    if configuration.upca:
        upc, _ = create_barcode_upca(configuration.barcode_path, configuration.upca)
    else:
        upc, _ = create_barcode(configuration.barcode_path)
    try:
        logo = Image.open(configuration.logo_path)
    except IOError:
        raise Exception("Error: Company logo not found.")
    try:
        barcode = Image.open(configuration.barcode_path)
    except IOError:
        raise Exception("Error: Barcode image not found.")
    logo = logo.resize((200, 200))
    left = 0
    upper = 50
    right = 200
    lower = 150
    box = (left, upper, right, lower)
    logo_cropped: ImageFile.ImageFile = logo.crop(box)

    barcode = barcode.resize((300, 150))
    left = 40
    upper = 15
    right = 260
    lower = 135
    box = (left, upper, right, lower)
    cropped: ImageFile.ImageFile = barcode.crop(box)

    label.paste(logo_cropped, (10, 10))
    label.paste(cropped, (10, 215))
    draw = ImageDraw.Draw(label)
    try:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype("arial.ttf", 32)
    except IOError:
        font = ImageFont.load_default()
    text_x = configuration.label_size[0] - 240
    text_y = 60
    line_spacing = 40
    for attribute in configuration.attributes:
        draw.text((text_x, text_y), attribute[0], font=font, fill="black")
        text_y += line_spacing
        draw.text((text_x, text_y), attribute[1], font=font, fill="black")
        text_y += line_spacing
    label.save(configuration.output_path)
    return upc
