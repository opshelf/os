import { IntermediateData } from "../CellSelectionForm";
import GantryController from "../GantryController";
import ToolController from "../ToolController";
import { CellSelectionForm } from "../CellSelectionForm";
import { useStateStore } from "@/store";
import { useState } from "react";
import { Card, CardContent } from "../ui/card";
import { Button } from "../ui/button";
import { robotCommand } from "@/api/robot";
import { RobotController as Controller } from "@/domain/robot";

/*
Store Item
Retrieve Item
Return Cell
Go to Output
Home
Stop
Idle
Initialize

Start Worker

*/

const RobotController = () => {
  const { robotStatus } = useStateStore();
  const [locationData, setLocationData] = useState<IntermediateData>({
    side: "left_module",
    shelf: null,
    cell: null,
  });

  const [placeLocationData, setPlaceLocationData] = useState<IntermediateData>({
    side: "left_module",
    shelf: null,
    cell: null,
  });
  const robot = new Controller(robotCommand);

  return (
    <div className="flex flex-col space-y-2">
      <div className="flex space-x-2">
        {robotStatus !== null && robotStatus.robot_status !== null && (
          <Card>
            <CardContent className="space-y-4">
              <CellSelectionForm
                data={robotStatus.robot_status.storage}
                setLocationData={setLocationData}
                locationData={locationData}
              />
              <Button
                onClick={async () => {
                  if (
                    locationData.cell !== null &&
                    locationData.shelf !== null
                  ) {
                    await robot.grabCell({
                      side: locationData.side,
                      shelf_idx: locationData.shelf,
                      cell_idx: locationData.cell,
                    });
                  }
                }}
              >
                Pick Cell
              </Button>
            </CardContent>
          </Card>
        )}
        {robotStatus !== null && robotStatus.robot_status !== null && (
          <Card>
            <CardContent className="space-y-4">
              <CellSelectionForm
                data={robotStatus.robot_status.storage}
                setLocationData={setPlaceLocationData}
                locationData={placeLocationData}
              />
              <Button
                onClick={async () => {
                  if (
                    placeLocationData.cell !== null &&
                    placeLocationData.shelf !== null
                  ) {
                    await robot.placeCell({
                      side: placeLocationData.side,
                      shelf_idx: placeLocationData.shelf,
                      cell_idx: placeLocationData.cell,
                    });
                  }
                }}
              >
                Place Cell
              </Button>
            </CardContent>
          </Card>
        )}
        {robotStatus !== null && robotStatus.robot_status !== null && (
          <Card>
            <CardContent className="space-y-4">
              <CellSelectionForm
                data={robotStatus.robot_status.storage}
                setLocationData={setPlaceLocationData}
                locationData={placeLocationData}
              />
              <Button
                onClick={async () => {
                  if (
                    placeLocationData.cell !== null &&
                    placeLocationData.shelf !== null
                  ) {
                    await robot.retrieveItem({
                      side: placeLocationData.side,
                      shelf_idx: placeLocationData.shelf,
                      cell_idx: placeLocationData.cell,
                    });
                  }
                }}
              >
                Retrieve Item
              </Button>
            </CardContent>
          </Card>
        )}
        {robotStatus !== null && robotStatus.robot_status !== null && (
          <Card>
            <CardContent className="space-y-4">
              <CellSelectionForm
                data={robotStatus.robot_status.storage}
                setLocationData={setPlaceLocationData}
                locationData={placeLocationData}
              />
              <Button
                onClick={async () => {
                  if (
                    placeLocationData.cell !== null &&
                    placeLocationData.shelf !== null
                  ) {
                    await robot.storeItem({
                      side: placeLocationData.side,
                      shelf_idx: placeLocationData.shelf,
                      cell_idx: placeLocationData.cell,
                    });
                  }
                }}
              >
                Store Item
              </Button>
            </CardContent>
          </Card>
        )}
        <Button
          onClick={async () => {
            await robot.home();
          }}
        >
          Home
        </Button>
        <Button
          onClick={async () => {
            await robot.idlePositionCommand();
          }}
        >
          Idle
        </Button>
        <Button
          onClick={async () => {
            await robot.moveToPickupLocation();
          }}
        >
          Move to Pickup
        </Button>
      </div>
      <GantryController />
      <ToolController />
    </div>
  );
};

export default RobotController;
