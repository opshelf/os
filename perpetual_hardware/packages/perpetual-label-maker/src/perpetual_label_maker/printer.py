from abc import abstractmethod, ABCMeta
import subprocess
from .events import LabelPrintPayload
from .label_maker import LabelInformation, LabelMaker


class AbstractPrinter(metaclass=ABCMeta):
    def print(self, filename: str, payload: LabelPrintPayload, label_information: LabelInformation = LabelInformation()) -> None:
        self._print(filename, payload, label_information)

    @abstractmethod
    def _print(self, filename: str, payload: LabelPrintPayload, label_information: LabelInformation = LabelInformation()) -> None: ...


class MockPrinter(AbstractPrinter):
    def _print(self, filename: str, payload: LabelPrintPayload, label_information: LabelInformation = LabelInformation()) -> None:
        print("printed", filename, payload, label_information)
        return None


class ThermalPrinter(AbstractPrinter):
    def __init__(self, printer_name: str = "ThermalPrinter") -> None:
        self._printer_name = printer_name
        self._label_maker = LabelMaker()

    def _print(
        self,
        filename: str,
        payload: LabelPrintPayload,
        label_information: LabelInformation = LabelInformation(),
    ) -> None:
        self._label_maker.create_label(payload, label_information)
        command = ["lp", "-d", self._printer_name, filename, "-o", "fit-to-page"]
        result = subprocess.run(command, capture_output=True, text=True)
        print(result)
        if result.returncode != 0:
            raise Exception(f"Print failed: {result.stderr}")
        return None
