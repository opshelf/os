"""Shared mass inputs and successful-cycle semantics (grams)."""
import math
from typing import Any
from ..database.models import Item
from .validation import ValidationError

MASS_FIELDS = ("tare_weight", "net_weight", "full_net_weight")
_MASS_LABELS = {"tare_weight": "container weight", "net_weight": "net weight", "full_net_weight": "full net weight"}

def validate_mass(values: dict[str, Any], tare: float | None = None) -> None:
    for key in MASS_FIELDS:
        value = values.get(key)
        if value is not None:
            try:
                number = float(value)
                valid = math.isfinite(number) and (number >= 0 if key == "net_weight" else number > 0)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValidationError(
                    f"Enter a {_MASS_LABELS[key]} of zero or more."
                    if key == "net_weight"
                    else f"Enter a {_MASS_LABELS[key]} greater than zero."
                )
    if "net_weight" in values:
        if "weight" in values:
            raise ValidationError("Enter either the gross weight or the net weight, not both.")
        if values["net_weight"] is not None and values.get("tare_weight", tare) is None:
            raise ValidationError("Enter the container weight together with the net weight.")
        if values["net_weight"] is not None and not math.isfinite(float(values["net_weight"]) + float(values.get("tare_weight", tare) or 0)):
            raise ValidationError("Total gross mass must be finite")

def apply_mass(item: Item, values: dict[str, Any], *, enrollment: bool = False) -> None:
    validate_mass(values, item.tare_weight)
    for key in ("tare_weight", "full_net_weight"):
        if key in values:
            setattr(item, key, float(values[key]) if values[key] is not None else None)
    if "net_weight" in values:
        item.weight = None if values["net_weight"] is None else float(values["net_weight"]) + float(item.tare_weight or 0)
        if enrollment:
            item.enrollment_weight_pending = values["net_weight"] is not None

def apply_cycle_mass(item: Item, weight: Any, *, stored: bool, command_id: str | None = None) -> bool:
    processed = item.processed_mass_commands or []
    if command_id and command_id in processed:
        return False
    protected = stored and item.enrollment_weight_pending
    if stored:
        item.enrollment_weight_pending = False
        item.enrollment_storage_completed = True
    if not protected and weight is not None:
        try:
            number = float(weight)
            if math.isfinite(number) and number >= 0:
                item.weight = number
        except (TypeError, ValueError):
            pass
    if command_id:
        item.processed_mass_commands = [*processed, command_id]
    return True
