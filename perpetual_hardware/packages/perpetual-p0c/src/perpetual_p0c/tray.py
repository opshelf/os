from pydantic import BaseModel
from typing import Tuple


class Tray(BaseModel):
    cells: int
    cell_width: float
    cell_depth: float
    wall_size: float = 5.0
    tray_size: float = 335.0

    def get_cell_position(self, idx: int) -> Tuple[float, float]:
        cell_width = (self.tray_size - (2 + self.cells - 1) * self.wall_size) / self.cells
        start_position = self.wall_size + cell_width * idx + self.wall_size * idx
        return (start_position, start_position + cell_width)

