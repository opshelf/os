from pathlib import Path

from openshelf_os_server.core.printer import LabelConfiguration, create_label


def test_create_label_uses_placeholder_when_logo_is_missing(tmp_path: Path) -> None:
    barcode_path = tmp_path / "barcode.png"
    output_path = tmp_path / "label.png"
    missing_logo_path = tmp_path / "missing-logo.png"

    upca = create_label(
        LabelConfiguration(
            upca="036000291452",
            logo_path=str(missing_logo_path),
            barcode_path=str(barcode_path),
            output_path=str(output_path),
            attributes=[("UPCA:", "036000291452")],
        )
    )

    assert upca == "036000291452"
    assert barcode_path.exists() is True
    assert output_path.exists() is True
