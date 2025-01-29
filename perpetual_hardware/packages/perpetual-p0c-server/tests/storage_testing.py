from perpetual_p0c.storage import CellItem, Dimensions, StorageGrid, CalibrationCoordinate, Shelf, Tray

if __name__ == '__main__':
    storage = StorageGrid(
            left_module=[],
            right_module=[
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        Tray(
                            width=150,
                            height=200,
                            calibration_point=CalibrationCoordinate(x=85, y=24, z=480),
                            slots=[],
                        ),
                    ],
                ),
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        Tray(
                            width=100,
                            height=150,
                            calibration_point=CalibrationCoordinate(x=115, y=274, z=480),
                            slots=[],
                        ),
                        Tray(width=100, height=200, slots=[]),
                    ],
                ),
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        #Tray(
                        #    width=50,
                        #    height=200,
                        #    calibration_point=CalibrationCoordinate(x=175, y=522, z=480),
                        #    slots=[],
                        #),
                        #Tray(width=50, height=150, slots=[]),
                        #Tray(width=50, height=100, slots=[]),
                    ],
                ),
            ],
        )
    storage.update(storage)
    serialized = storage.model_dump_json()
    deserialized = StorageGrid.model_validate_json(serialized)
