import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '../ui/card';
import { RobotController as Robot } from '@/domain/robot';
import { CellSelectionForm } from '../CellLocationForm';
import { Label } from '../ui/label';
import { PullTrayCommand } from '@/domain/commands/robot';
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
  CornerDownLeft,
  EraserIcon,
  GrabIcon,
  HomeIcon,
  LoaderIcon,
  LocateIcon,
  RotateCcwIcon,
  // ReplaceIcon,
  StopCircleIcon,
  WrenchIcon,
} from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectValue,
  SelectTrigger,
} from '../ui/select';
import { StorageModuleData } from '@/domain';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import CommandList from '../CommandList';

const PullTrayForm = ({ data }: { data: StorageModuleData }) => {
  const [side, setSide] = useState<StorageModuleSide>('right_module');
  const [shelf, setShelf] = useState<number | null>(1);
  const [tray, setTray] = useState<number | null>(0);

  const pull = async () => {
    if (shelf !== null && tray !== null) {
      let command: PullTrayCommand = {
        ...createBaseMessage(),
        command: 'pull_tray',
        payload: {
          side,
          shelf_idx: shelf,
          tray_idx: tray,
        },
      };
      await robotCommand(command);
    }
  };
  return (
    <Card className="w-fit">
      <CardHeader>
        <CardTitle>Pull Tray</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col space-y-2 items-start">
        <Label className="text-lg">Side</Label>
        <Select
          value={side}
          onValueChange={(value) => {
            setSide(value as StorageModuleSide);
            if (data[value as StorageModuleSide].length) {
              setShelf(0);
            } else {
              setShelf(null);
            }
          }}
        >
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="left_module">Left Module</SelectItem>
            <SelectItem value="right_module">Right Module</SelectItem>
          </SelectContent>
        </Select>
        {data[side].length > 0 && (
          <>
            <Label className="text-lg">Shelf</Label>
            <Select
              value={`${shelf}`}
              onValueChange={(value) => setShelf(parseInt(value))}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {data[side].map((_, shelf_idx) => {
                  return (
                    <SelectItem
                      value={`${shelf_idx}`}
                      key={`${shelf_idx}-pull-shelf`}
                    >
                      {`${shelf_idx}`}
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
            {shelf !== null && data[side][shelf].trays.length > 0 && (
              <>
                <Label className="text-lg">Tray</Label>
                <Select
                  value={`${tray}`}
                  onValueChange={(value) => setTray(parseInt(value))}
                >
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {shelf &&
                      data[side][shelf].trays.map((_, trayIdx) => {
                        return (
                          <SelectItem
                            value={`${trayIdx}`}
                            key={`${trayIdx}-pull-tray`}
                          >
                            {`${trayIdx}`}
                          </SelectItem>
                        );
                      })}
                  </SelectContent>
                </Select>
              </>
            )}
          </>
        )}
        {shelf !== null && tray !== null && (
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
    <Card>
      <CardHeader>
        <CardTitle>Store Item</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <Label className="text-xl">Store in Location</Label>
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
                Add Item
              </Button>
            )}
        </div>
      </CardContent>
    </Card>
  );
};

/*
<Button>
  <ReplaceIcon />
  Pull Tray
</Button>
class StorePayload(BaseModel):
    location: StorageModuleCellLocation
    item: Optional[CellItem] = None


class StoreCommand(BaseMessage):
    command: Literal["store_item"]
    payload: StorePayload


class RetrievePayload(BaseModel):
    location: StorageModuleCellLocation
    item: Optional[CellItem]


class RetrieveCommand(BaseMessage):
    command: Literal["retrieve_item"]
    payload: RetrievePayload

*/
/*
const StoreItemForm = ({ data }: { data: StorageModuleData }) => {
  const [side, setSide] = useState<StorageModuleSide>('right_module');
  const [shelf, setShelf] = useState<number | null>(null);
  const [tray, setTray] = useState<number | null>(null);
  const [cell, setCell] = useState<number | null>(null);

  //const pull = async () => {
  //  if (shelf && tray) {
  //    let command: PullTrayCommand = {
  //      ...createBaseMessage(),
  //      command: 'pull_tray',
  //      payload: {
  //        side,
  //        shelf_idx: shelf,
  //        tray_idx: tray,
  //      },
  //    };
  //    await robotCommand(command);
  //  }
  //};

  return (
    <Card className="w-fit">
      <CardHeader>
        <CardTitle>Store Item</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col space-y-2 items-start">
        <Label className="text-lg">Side</Label>
        <Select
          value={side}
          onValueChange={(value) => {
            setSide(value as StorageModuleSide);
            if (data[value as StorageModuleSide].length) {
              setShelf(0);
            } else {
              setShelf(null);
            }
          }}
        >
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="left_module">Left Module</SelectItem>
            <SelectItem value="right_module">Right Module</SelectItem>
          </SelectContent>
        </Select>
        {data[side].length > 0 && (
          <>
            <Label className="text-lg">Shelf</Label>
            <Select
              value={`${shelf}`}
              onValueChange={(value) => setShelf(parseInt(value))}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {data[side].map((_, shelf_idx) => {
                  return (
                    <SelectItem
                      value={`${shelf_idx}`}
                      key={`${shelf_idx}-pull-shelf`}
                    >
                      {shelf_idx}
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
            {shelf !== null && data[side][shelf].trays.length > 0 && (
              <>
                <Label className="text-lg">Tray</Label>
                <Select
                  value={`${tray}`}
                  onValueChange={(value) => setTray(parseInt(value))}
                >
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {shelf &&
                      data[side][shelf].trays.map((_, trayIdx) => {
                        return (
                          <SelectItem
                            value={`${trayIdx}`}
                            key={`${trayIdx}-pull-tray`}
                          >
                            {trayIdx}
                          </SelectItem>
                        );
                      })}
                  </SelectContent>
                </Select>
              </>
            )}
          </>
        )}
        {shelf !== null && tray !== null && (
          <Button>
            <BetweenHorizonalStartIcon />
            Pull Tray
          </Button>
        )}
      </CardContent>
      <CardFooter></CardFooter>
    </Card>
  );
};

const RetrieveItemForm = () => {};
*/
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
