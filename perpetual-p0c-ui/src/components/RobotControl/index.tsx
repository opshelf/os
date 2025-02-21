import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '../ui/card';
import { RobotController as Robot } from '@/domain/robot';
import { CellSelectionForm } from '../CellLocationForm';
import { getStorage } from '@/api';
import { StorageModuleSide } from '@/domain';
import {
  robotCommand,
  createBaseMessage,
  startWorker,
  clearCommands,
} from '@/api/robot';
import { Button } from '../ui/button';
import {
  ArchiveRestoreIcon,
  CornerDownLeft,
  EraserIcon,
  GrabIcon,
  HomeIcon,
  LoaderIcon,
  LocateIcon,
  RotateCcwIcon,
  StopCircleIcon,
  WrenchIcon,
} from 'lucide-react';
import { TraySelectionForm } from '../TrayLocationForm';
import { StorageModuleData } from '@/domain';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import CommandList from '../CommandList';
import { RobotController as RobotControllerApi } from '@/domain/robot';

const PullTrayForm = ({ data }: { data: StorageModuleData }) => {
  const [locationData, setLocationData] = useState<{
    side: StorageModuleSide;
    shelf: number | null;
    tray: number | null;
  }>({
    side: 'right_module',
    shelf: null,
    tray: null,
  });

  const robot = new RobotControllerApi(robotCommand);

  const pull = async () => {
    if (locationData.shelf !== null && locationData.tray !== null) {
      await robot.pullTray({
        side: locationData.side,
        shelf_idx: locationData.shelf,
        tray_idx: locationData.tray,
      });
    }
  };

  return (
    <Card className="w-72">
      <CardHeader>
        <CardTitle>Pull Tray</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col space-y-2 items-start">
        <TraySelectionForm
          data={data}
          locationData={locationData}
          setLocationData={setLocationData}
        />
        {locationData.shelf !== null &&
          locationData.tray !== null && (
            <Button onClick={async () => await pull()}>
              <GrabIcon />
              Pull Tray
            </Button>
          )}
      </CardContent>
      <CardFooter></CardFooter>
    </Card>
  );
};

const StoreForm = ({ data }: { data: StorageModuleData }) => {
  const [locationData, setLocationData] = useState<{
    side: StorageModuleSide;
    shelf: number | null;
    tray: number | null;
    cell: number | null;
  }>({
    side: 'right_module',
    shelf: null,
    tray: null,
    cell: null,
  });
  return (
    <Card className="w-72">
      <CardHeader>
        <CardTitle>Store Item</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex flex-col space-y-2">
          <CellSelectionForm
            data={data}
            setLocationData={setLocationData}
            locationData={locationData}
          />
          {locationData.shelf !== null &&
            locationData.tray !== null &&
            locationData.cell !== null && (
              <Button
                onClick={async () => {
                  await robotCommand({
                    ...createBaseMessage(),
                    command: 'store_item',
                    payload: {
                      location: {
                        side: locationData.side,
                        tray_idx: locationData.tray as number,
                        shelf_idx: locationData.shelf as number,
                        slot_idx: locationData.cell as number,
                      },
                      item: null,
                    },
                  });
                }}
              >
                <ArchiveRestoreIcon />
                Add Item
              </Button>
            )}
        </div>
      </CardContent>
    </Card>
  );
};

const RobotController = () => {
  const { isPending, isError, data, error } = useQuery({
    queryKey: ['storage'],
    queryFn: getStorage,
  });

  const robot = new Robot(robotCommand);

  if (isPending) {
    return <span>Loading...</span>;
  }

  if (isError) {
    return <span>Error: {error.message}</span>;
  }

  return (
    <div className="flex flex-col space-y-4 w-full">
      <div className="flex space-x-4">
        <Button
          onClick={async () => {
            await robot.home();
          }}
        >
          <HomeIcon />
          Home
        </Button>
        <Button
          onClick={async () => {
            await robot.homeYFirst();
          }}
        >
          <HomeIcon />
          Home Y First
        </Button>
        <Button
          onClick={async () => {
            await robot.initialize();
          }}
        >
          <LoaderIcon />
          Initialize
        </Button>
        <Button
          onClick={async () => {
            await robot.returnCurrentTray();
          }}
        >
          <CornerDownLeft />
          Return Tray
        </Button>
        <Button
          onClick={async () => {
            await robot.idlePositionCommand();
          }}
        >
          <LocateIcon />
          Idle
        </Button>
        <Button
          onClick={async () => {
            await robot.serviceHighPosition();
          }}
        >
          <WrenchIcon />
          Service High Position
        </Button>
        <div className="grow" />
        <Button
          variant={'outline'}
          onClick={async () => {
            await startWorker();
          }}
        >
          <RotateCcwIcon />
          Restart
        </Button>
        <Button
          variant={'secondary'}
          onClick={async () => {
            await clearCommands();
          }}
        >
          <EraserIcon />
          Clear Commands
        </Button>
        <Button
          variant={'destructive'}
          onClick={async () => {
            await robotCommand({
              ...createBaseMessage(),
              command: 'stop',
            });
          }}
        >
          <StopCircleIcon />
          Stop
        </Button>
      </div>
      <div className="flex space-x-4">
        <PullTrayForm data={data} />
        <StoreForm data={data} />
      </div>
      <CommandList />
    </div>
  );
};

export default RobotController;
