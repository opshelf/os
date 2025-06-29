from pydantic import BaseModel
from typing import Optional, List

class LocalizedResult(BaseModel):
    object_size: float
    first_break_location: float
    last_break_location: float

class Localizer:

    def __init__(self, beam_distance_mm: float, deadspace: float, reverse: bool = True) -> None:
        self._beam_distance_mm = beam_distance_mm
        self._deadspace = deadspace
        self._reverse = reverse
        self._padding = self._beam_distance_mm * 2

    def localize(self, beams: List[int]) -> Optional[LocalizedResult]:
        first_break = None
        last_break = None
        if self._reverse:
            beams.reverse()
        for idx, beam in enumerate(beams):
            if first_break is None:
                if beam:
                    first_break = idx
            else:
                if beam:
                    last_break = idx
        if first_break is None:
            return None
        elif first_break is not None and last_break is not None:
            if first_break == 0:
                return LocalizedResult(
                    object_size=(last_break-first_break)*self._beam_distance_mm + self._deadspace + self._padding/2,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace,
                    last_break_location=last_break*self._beam_distance_mm + self._deadspace + self._padding/2
                )
            else:
                return LocalizedResult(
                    object_size=(last_break-first_break)*self._beam_distance_mm + self._padding,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace  - self._padding/2,
                    last_break_location=last_break*self._beam_distance_mm + self._deadspace + self._padding/2
                )
        elif first_break is not None:
            if first_break == 0:
                return LocalizedResult(
                    object_size=first_break*self._beam_distance_mm + self._deadspace + self._padding/2,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace,
                    last_break_location=self._beam_distance_mm + self._deadspace
                )
            else:
                return LocalizedResult(
                    object_size=20,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace + self._padding,
                    last_break_location=first_break*self._beam_distance_mm + self._deadspace,
                )
        else: 
            return None
