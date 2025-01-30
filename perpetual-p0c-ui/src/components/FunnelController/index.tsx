import { useEffect, useState } from 'react';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectValue,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { robotCommand } from '@/api/robot';
import { RobotController } from '@/domain/robot';
import { CellData, Tray } from '@/domain';
import { useStateStore } from '@/store';

const Controls = ({
  tray,
  setTray,
}: {
  tray: Tray;
  setTray: (tray: Tray) => void;
}) => {
  const robot = new RobotController(robotCommand);
  const getValueForSlots = (slotLength: number): string => {
    switch (slotLength) {
      case 2:
        return '150';
      case 3:
        return '100';
      case 6:
        return '50';
      default:
        return '50';
    }
  };
  return (
    <Card className="grow">
      <CardHeader>
        <CardTitle>Controls</CardTitle>
        <CardDescription>Funnel Controls</CardDescription>
      </CardHeader>
      <CardContent className="w-full grid grid-rows-2 gap-2">
        <div className="flex w-full items-center space-x-2">
          <p className="text-xl text-nowrap">Tray Size:</p>
          <Select
            value={getValueForSlots(tray.slots.length)}
            onValueChange={(value) => {
              const size = parseInt(value);
              let newSlots: CellData[] = [
                {
                  status: 'EMPTY',
                  item: null,
                },
                {
                  status: 'EMPTY',
                  item: null,
                },
                {
                  status: 'EMPTY',
                  item: null,
                },
                {
                  status: 'EMPTY',
                  item: null,
                },
                {
                  status: 'EMPTY',
                  item: null,
                },
                {
                  status: 'EMPTY',
                  item: null,
                },
              ];
              if (size == 150) {
                newSlots.splice(2);
              } else if (size == 100) {
                newSlots.splice(3);
              }
              setTray({
                ...tray,
                width: size,
                slots: newSlots,
              });
            }}
          >
            <SelectTrigger className="w-32">
              <SelectValue></SelectValue>
            </SelectTrigger>
            <SelectContent className="w-32">
              <SelectItem value={'50'}>50 mm</SelectItem>
              <SelectItem value={'100'}>100 mm</SelectItem>
              <SelectItem value={'150'}>150 mm</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button
            onClick={async () => await robot.funnel.homeCommand()}
          >
            HOME
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.startBeltCommand()
            }
          >
            START INPUT
          </Button>
          <Button
            onClick={async () => await robot.funnel.stopBeltCommand()}
          >
            STOP INPUT
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.startWallCommand()
            }
          >
            START WALL
          </Button>
          <Button
            onClick={async () => await robot.funnel.stopWallCommand()}
          >
            STOP WALL
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.startOutputCommand()
            }
          >
            START OUTPUT
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.stopOutputCommand()
            }
          >
            STOP OUTPUT
          </Button>

          <Button
            onClick={async () =>
              await robot.funnel.startOutputTransferCommand()
            }
          >
            START TRANSFER
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.stopOutputTransferCommand()
            }
          >
            STOP TRANSFER
          </Button>
          <Button
            onClick={async () => await robot.funnel.stopCommand()}
          >
            STOP
          </Button>
          <Button
            onClick={async () =>
              await robot.funnel.initializeCommand()
            }
          >
            INITIALIZE
          </Button>
        </div>
      </CardContent>
    </Card>
  );
};

function FunnelController() {
  const [tray, setTray] = useState<Tray>({
    height: 150,
    width: 150,
    calibration_point: null,
    empty: false,
    slots: [
      {
        status: 'EMPTY',
        item: null,
      },
      {
        status: 'EMPTY',
        item: null,
      },
      {
        status: 'EMPTY',
        item: null,
      },
      {
        status: 'EMPTY',
        item: null,
      },
      {
        status: 'EMPTY',
        item: null,
      },
      {
        status: 'EMPTY',
        item: null,
      },
    ],
  });

  const [selectedCell, setSelectedCell] = useState(0);
  const robot = new RobotController(robotCommand);
  const { robotStatus } = useStateStore();

  const funnelStatus = robotStatus?.robot_status?.funnel_status;

  useEffect(() => {
    if (selectedCell >= tray.slots.length) {
      setSelectedCell(0);
    }
  }, [tray, selectedCell]);

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Funnel Control</CardTitle>
      <CardContent className="px-0 relative">
        <div className="flex items-stretch space-x-4">
          <Controls tray={tray} setTray={setTray} />
        </div>
        <div className="flex flex-col items-center justify-center pt-8">
          <span className="text-lg font-semibold">Tray Layout</span>
          <div className="flex space-x-2 w-[720px] h-72 items-end">
            {Array(tray.slots.length)
              .fill(0)
              .map((_r, cell_idx) => {
                return (
                  <div
                    style={{
                      width: `${
                        (720 - (tray.slots.length - 1) * 2) /
                        tray.slots.length
                      }px`,
                      height: `${64 * 0.25 * (tray.width / 150)}rem`,
                    }}
                    className={`block h-32 border border-slate-100 ${
                      cell_idx === selectedCell
                        ? 'bg-slate-300'
                        : 'bg-slate-50'
                    } rounded`}
                    onClick={async () => {
                      setSelectedCell(cell_idx);
                      await robot.funnel.moveToCellCommand(
                        tray,
                        cell_idx
                      );
                    }}
                  ></div>
                );
              })}
          </div>
        </div>
        <div className="flex w-full absolute bottom-0 space-x-4">
          {funnelStatus !== undefined && (
            <>
              <p>
                Inlet Funnel: {funnelStatus.inlet_funnel.position}
              </p>
              <p>
                Opposing Funnel:{' '}
                {funnelStatus.opposing_funnel.position}
              </p>
              <p>Slot 1: {funnelStatus.slot_1.position}</p>
              <p>Slot 2: {funnelStatus.slot_2.position}</p>
              <p>Slot 3: {funnelStatus.slot_3.position}</p>
              <p>Diverter: {funnelStatus.diverter.position}</p>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default FunnelController;
