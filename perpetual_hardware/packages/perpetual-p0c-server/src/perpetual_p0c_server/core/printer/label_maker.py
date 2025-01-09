from PIL import Image, ImageDraw, ImageFont
from pylibdmtx.pylibdmtx import encode
import os
from perpetual_p0c_server.core.events import LabelPrintPayload

#font_paths = []
#
## macOS
#font_paths.extend(os.listdir("/System/Library/Fonts"))
#font_paths.extend(os.listdir("/Library/Fonts"))
#
## Filter for TrueType fonts
#font_paths = [font for font in font_paths if font.endswith(".ttf")]

def create_prescription_label(
    label_data: LabelPrintPayload
) -> None:
    patient_name="Doe, Jane"
    medication_name="METFORMIN HCL"
    medication_strength="500 MG"
    medication_form="TABLET"
    generic_equivalent="Brand Name"
    instructions="Take 1 tablet orally\n2 times a day"
    pharmacy_name="Pharmacy"
    pharmacy_address="555 Main St\nNew York NY 10038"
    prescriber_name="Dr. John Smith"
    date_filled="MM/DD/YYYY"
    rph_name="Sarah Williams"
    manufacturer_name="Manufacturer"
    output_file="prescription_label.png"
    # Image dimensions
    width, height = 800, 600
    background_color = "white"
    text_color = "black"

    # Create blank image
    img = Image.new("RGB", (width, height), background_color)
    draw = ImageDraw.Draw(img, "RGBA")
    # Load fonts (You may need to adjust font paths)
    from sys import platform
    if platform == "linux" or platform == "linux2":
        font_extra_bold = ImageFont.truetype("Lato-Medium.ttf", 40)
        font_bold = ImageFont.truetype("Lato-Medium.ttf", 32)
        font_regular = ImageFont.truetype("Lato-Medium.ttf", 24)
    elif platform == "darwin":
        font_extra_bold = ImageFont.truetype("Arial Unicode.ttf", 40)
        font_bold = ImageFont.truetype("Arial Unicode.ttf", 32)
        font_regular = ImageFont.truetype("Arial Unicode.ttf", 24)
    else:
        raise Exception("Invalid Platform")


    # Layout
    padding = 20
    section_spacing = 40

    # Left section (Patient and medication details)
    x_left = padding
    y = padding

    # Patient name
    draw.text((x_left, y), f"{patient_name}", fill=text_color, font=font_bold)

    # Medication details
    draw.text(
        (x_left, 220),
        f"{medication_name}",
        fill=text_color,
        font=font_extra_bold,
    )
    draw.text(
        (x_left, 270),
        f"{medication_strength} {medication_form}",
        fill=text_color,
        font=font_extra_bold,
    )
    draw.text(
        (x_left, 320),
        f"Generic equivalent of {generic_equivalent}",
        fill=text_color,
        font=font_regular,
    )

    # Instructions
    draw.text((x_left, 460), 
              instructions, 
              fill=text_color, 
              font=font_regular
             )

    # Right section (Pharmacy and prescriber details)
    x_right = width // 2 + 140

    # Pharmacy name and address
    draw.text((x_right, y), pharmacy_name, fill=text_color, font=font_bold)
    y += section_spacing
    draw.text((x_right, y), pharmacy_address, fill=text_color, font=font_regular)
    y += section_spacing*2

    # Prescriber details
    draw.text(
        (x_right, y), 
        f"Prescriber:", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"{prescriber_name}", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"Date filled:", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"{date_filled}", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"RPH:", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"{rph_name}", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"MFR:", fill=text_color, font=font_regular
    )
    y += section_spacing
    draw.text(
        (x_right, y), 
        f"{manufacturer_name}", fill=text_color, font=font_regular
    )


    encoded = encode(label_data.gs1.encode('utf8'))
    dm_img = Image.frombytes('RGB', (encoded.width, encoded.height), encoded.pixels)
    dm_img = dm_img.resize((100, 100))
    img.paste(dm_img, (width - 120, height - 120))  # Bottom-right corner
    

    logo = Image.open("./perpetual-logo-vector.jpg")
    img.paste(logo, (20, height-65))
    draw.rounded_rectangle(
        (10, 10, width-10, height-20),
        fill=(0, 0, 0, 0),
        outline="black",
        width=2,
        radius=8
    )

    # Save the image
    img.save(output_file)
    print(f"Prescription label saved to {output_file}")
