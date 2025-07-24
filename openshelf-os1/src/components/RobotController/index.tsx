import { IntermediateData } from "../CellSelectionForm";
import GantryController from "../GantryController";
import ToolController from "../ToolController";
import { CellSelectionForm } from "../CellSelectionForm";
import { useStateStore } from "@/store";
import { useState } from "react";
import { Card, CardContent } from "../ui/card";
import { Button } from "../ui/button";
import { robotCommand, startWorker, clearCommands } from "@/api/robot";
import { RobotController as Controller } from "@/domain/robot";
import {
  RotateCcwIcon,
  EraserIcon,
  StopCircleIcon,
  HomeIcon,
  LocateIcon,
  WeightIcon,
  LoaderIcon,
  CornerDownLeft,
} from "lucide-react";
import DimensionForm from "../DimensionsForm";
import { handleApiCall } from "@/api/errorHandler";

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
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.home(),
              "Failed to home robot"
            );
          }}
        >
          <HomeIcon />
          Home
        </Button>
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.idlePositionCommand(),
              "Failed to move to idle position"
            );
          }}
        >
          <LocateIcon />
          Idle
        </Button>
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.moveToPickupLocation(),
              "Failed to move to pickup location"
            );
          }}
        >
          <WeightIcon />
          Move to Pickup
        </Button>
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.initialize(),
              "Failed to initialize robot"
            );
          }}
        >
          <LoaderIcon />
          Initialize
        </Button>
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.returnCell(),
              "Failed to return cell"
            );
          }}
        >
          <CornerDownLeft /> Return Cell
        </Button>
        <Button
          onClick={async () => {
            await handleApiCall(
              () => robot.stop(),
              "Failed to stop robot"
            );
          }}
          variant={"destructive"}
        >
          <StopCircleIcon /> Stop
        </Button>
        <Button
          variant={"outline"}
          onClick={async () => {
            await handleApiCall(
              () => startWorker(),
              "Failed to restart worker"
            );
          }}
        >
          <RotateCcwIcon />
          Restart
        </Button>
        <Button
          variant={"secondary"}
          onClick={async () => {
            await handleApiCall(
              () => clearCommands(),
              "Failed to clear commands"
            );
          }}
        >
          <EraserIcon />
          Clear Commands
        </Button>
      </div>
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
                    await handleApiCall(
                      () => robot.grabCell({
                        side: locationData.side,
                        shelf_idx: locationData.shelf,
                        cell_idx: locationData.cell,
                      }),
                      `Failed to pick cell at ${locationData.side} shelf ${locationData.shelf} cell ${locationData.cell}`
                    );
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
                    await handleApiCall(
                      () => robot.placeCell({
                        side: placeLocationData.side,
                        shelf_idx: placeLocationData.shelf,
                        cell_idx: placeLocationData.cell,
                      }),
                      `Failed to place cell at ${placeLocationData.side} shelf ${placeLocationData.shelf} cell ${placeLocationData.cell}`
                    );
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
      </div>
      <GantryController />
      <ToolController />
      <DimensionForm />
    </div>
  );
};

export default RobotController;
