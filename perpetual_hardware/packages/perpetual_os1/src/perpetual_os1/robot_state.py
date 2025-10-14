from enum import Enum
from typing import Optional
from perpetual_os1.iobox import IOBoxStatus
from perpetual_os1.tool import ToolItemStatus, ToolMotionStatus, ToolStatus

class RobotStateError(Exception):
    """Raised when an operation is not allowed in the current robot state."""
    pass

class RobotOperationState(Enum):
    """
    High-level robot operation states based on Tool and IOBox conditions.
    """
    IDLE = "idle"                      # No item in tool or IOBox
    ITEM_IN_IOBOX = "item_in_box"      # Item present in IOBox, tool empty
    ITEM_IN_TOOL = "item_in_tool"      # Item held by tool, IOBox empty
    BOTH_OCCUPIED = "both_occupied"     # Items present in both tool and IOBox
    ERROR = "error"                    # Error state in either component

class RobotStateManager:
    """
    Manages allowed operations based on Tool and IOBox states.
    
    State Transitions:
    - IDLE -> ITEM_IN_IOBOX: Item placed in IOBox
    - ITEM_IN_IOBOX -> ITEM_IN_TOOL: Tool picks up item from IOBox
    - ITEM_IN_TOOL -> IDLE: Tool places item in storage
    - ITEM_IN_TOOL -> ITEM_IN_IOBOX: Tool places item in IOBox
    - ITEM_IN_TOOL -> BOTH_OCCUPIED: New item placed in IOBox while tool holds item
    - ITEM_IN_IOBOX -> BOTH_OCCUPIED: Tool picks item from storage while IOBox has item
    - BOTH_OCCUPIED -> ITEM_IN_TOOL: Tool places IOBox item in storage
    - BOTH_OCCUPIED -> ITEM_IN_IOBOX: Tool places its item in storage
    - Any state -> ERROR: Component error
    """
    
    def __init__(self):
        self._current_state = RobotOperationState.IDLE

    @property
    def current_state(self) -> RobotOperationState:
        return self._current_state

    def update_state(self, tool_status: ToolStatus, io_status: IOBoxStatus) -> RobotOperationState:
        """
        Update state machine based on tool and IOBox status.
        Returns the new state.
        """
        # Check for error conditions first
        if (tool_status.motion == ToolMotionStatus.ERROR or 
            tool_status.item == ToolItemStatus.ERROR or 
            io_status == IOBoxStatus.ERROR):
            self._current_state = RobotOperationState.ERROR
            return self._current_state

        # Determine state based on item locations
        tool_has_item = tool_status.item == ToolItemStatus.LOADED
        iobox_has_item = io_status == IOBoxStatus.READY

        if tool_has_item and iobox_has_item:
            self._current_state = RobotOperationState.BOTH_OCCUPIED
        elif tool_has_item:
            self._current_state = RobotOperationState.ITEM_IN_TOOL
        elif iobox_has_item:
            self._current_state = RobotOperationState.ITEM_IN_IOBOX
        else:
            self._current_state = RobotOperationState.IDLE

        return self._current_state

    def can_measure_item(self) -> bool:
        """Check if item measurement is allowed."""
        # Can measure when item is in IOBox, regardless of tool state
        return self._current_state in [RobotOperationState.ITEM_IN_IOBOX, RobotOperationState.BOTH_OCCUPIED]

    def can_pick_from_iobox(self) -> bool:
        """Check if picking item from IOBox is allowed."""
        # Can only pick from IOBox when tool is empty
        return self._current_state == RobotOperationState.ITEM_IN_IOBOX

    def can_place_in_iobox(self) -> bool:
        """Check if placing item in IOBox is allowed."""
        # Can only place in IOBox when tool has item and IOBox is empty
        return self._current_state == RobotOperationState.ITEM_IN_TOOL

    def can_pick_from_storage(self) -> bool:
        """Check if picking item from storage is allowed."""
        # Can pick from storage when system is idle or when IOBox has an item
        return self._current_state in [RobotOperationState.IDLE, RobotOperationState.ITEM_IN_IOBOX]

    def can_place_in_storage(self) -> bool:
        """Check if placing item in storage is allowed."""
        # Can place in storage when tool has item, regardless of IOBox state
        return self._current_state in [RobotOperationState.ITEM_IN_TOOL, RobotOperationState.BOTH_OCCUPIED]

    def can_store_item(self) -> bool:
        """Check if store operation is allowed."""
        return self._current_state == RobotOperationState.ITEM_IN_IOBOX

    def can_retrieve_item(self) -> bool:
        """Check if retrieve operation is allowed."""
        return self._current_state == RobotOperationState.IDLE

    def validate_operation(self, operation: str) -> None:
        """
        Validate if an operation is allowed in the current state.
        Raises RobotStateError if operation is not allowed.
        """
        validation_map = {
            'measure': self.can_measure_item,
            'pick_iobox': self.can_pick_from_iobox,
            'place_iobox': self.can_place_in_iobox,
            'pick_storage': self.can_pick_from_storage,
            'place_storage': self.can_place_in_storage,
            'store': self.can_store_item,
            'retrieve': self.can_retrieve_item
        }

        check_func = validation_map.get(operation)
        if check_func is None:
            raise ValueError(f"Unknown operation: {operation}")

        if not check_func():
            raise RobotStateError(
                f"Operation '{operation}' is not allowed in state {self._current_state.value}"
            )