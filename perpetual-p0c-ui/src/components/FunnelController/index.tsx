import { useState } from 'react';
import { Slider } from '@/components/ui/slider';
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
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { MinusIcon, PlusIcon } from 'lucide-react';
import { funnelCommand } from '@/api';
//import z from 'zod';
//import {
//  FunnelSubsytemCommands,
//  FunnelBeltCommands,
//  FunnelCommands,
//} from '@/domain/funnel';

export type TrayConfiguration = {
  cells: number;
  cell_depth: number;
  cell_width: number;
};

const Controls = ({
  tray,
  idx,
}: {
  tray: TrayConfiguration;
  idx: number;
}) => {
  console.log(tray, idx);
  const [wiggleDistance, setWiggleDistance] = useState(0.2);
  return (
    <Card className="grow">
      <CardHeader>
        <CardTitle>Controls</CardTitle>
        <CardDescription>Funnel Controls</CardDescription>
      </CardHeader>
      <CardContent className="w-full grid grid-rows-2 gap-2">
        <div className="flex gap-2 flex-wrap">
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'HOME',
                payload: null,
              })
            }
          >
            HOME
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'START_INLET_BELT',
                payload: null,
              })
            }
          >
            START INLET
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'STOP_INLET_BELT',
                payload: null,
              })
            }
          >
            STOP INLET
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'START_WALL_BELT',
                payload: null,
              })
            }
          >
            START WALL
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'STOP_WALL_BELT',
                payload: null,
              })
            }
          >
            STOP WALL
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'START_OUTPUT_BELT',
                payload: null,
              })
            }
          >
            START OUTPUT
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'STOP_OUTPUT_BELT',
                payload: null,
              })
            }
          >
            STOP OUTPUT
          </Button>

          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'START_TRANSFER_BELT',
                payload: null,
              })
            }
          >
            START TRANSFER
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'STOP_TRANSFER_BELT',
                payload: null,
              })
            }
          >
            STOP TRANSFER
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'STOP',
                payload: null,
              })
            }
          >
            STOP
          </Button>
          {/*
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'ACCUMULATE',
                payload: { tray, idx },
              })
            }
          >
            ACCUMULATE
          </Button>

          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'DROP',
                payload: { tray, idx },
              })
            }
          >
            DROP
          </Button>
          */}
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'CLOSE_FUNNEL',
                payload: null,
              })
            }
          >
            CLOSE
          </Button>
          <Button
            onClick={async () =>
              await funnelCommand({
                command: 'WIGGLE',
                payload: { wiggle: wiggleDistance },
              })
            }
          >
            WIGGLE
          </Button>
        </div>
        <div className="flex flex-col space-y-2">
          <Label>Wiggle {wiggleDistance} (mm)</Label>
          <Slider
            min={0}
            max={1}
            step={0.1}
            value={[wiggleDistance]}
            onValueChange={(v) => setWiggleDistance(v[0])}
          />
        </div>
      </CardContent>
    </Card>
  );
};

export const Conveyor = () => {
  return (
    <Card className="">
      <CardHeader>
        <CardTitle>Conveyor Belt</CardTitle>
        <CardDescription>Conveyor Belt Controls</CardDescription>
      </CardHeader>
      <CardContent className="w-full flex space-x-4">
        <div className="grid grid-rows-2">
          <Label>Conveyor Belt Speed (mm/s)</Label>
          <Slider min={0} max={100} defaultValue={[50]} step={1} />
        </div>
        <div className="grid gap-2">
          <Label>Conveyor On</Label>
          <Switch />
        </div>
      </CardContent>
    </Card>
  );
};

const TrayConfigurator = ({
  tray,
  setTray,
}: {
  tray: TrayConfiguration;
  setTray: (tray: TrayConfiguration) => void;
}) => {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Tray Configurator</CardTitle>
        <CardDescription>Tray Configuration Tool</CardDescription>
      </CardHeader>
      <CardContent className="w-full flex-col space-y-2">
        <div className="w-full flex space-x-2">
          <div className="flex flex-col space-y-2">
            <Label>Cells</Label>
            <div className="flex items-center space-x-2">
              <Button
                size={'sm'}
                onClick={() =>
                  setTray({
                    ...tray,
                    cells: Math.max(tray.cells - 1, 0),
                  })
                }
              >
                <MinusIcon />
              </Button>
              <p className="w-8 text-center text-lg">{tray.cells}</p>
              <Button
                size={'sm'}
                onClick={() =>
                  setTray({ ...tray, cells: tray.cells + 1 })
                }
              >
                <PlusIcon />
              </Button>
            </div>
          </div>
          <div className="flex flex-col space-y-2">
            <Label>Cell Depth (mm)</Label>
            <Select
              onValueChange={(v) =>
                setTray({ ...tray, cell_depth: parseInt(v) })
              }
              value={`${tray.cell_depth}`}
              defaultValue={`50`}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="50">50</SelectItem>
                <SelectItem value="100">100</SelectItem>
                <SelectItem value="150">150</SelectItem>
                <SelectItem value="200">200</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col space-y-2">
            <Label>Cell Width (mm)</Label>
            <Select
              onValueChange={(v) =>
                setTray({ ...tray, cell_width: parseInt(v) })
              }
              value={`${tray.cell_width}`}
              defaultValue={`50`}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="50">50</SelectItem>
                <SelectItem value="100">100</SelectItem>
                <SelectItem value="150">150</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex space-x-2">
          <b>Cell Size:</b>
          <p>
            {(335.0 - (tray.cells - 1) * 5.0 - 10.0) / tray.cells} mm
          </p>
        </div>
      </CardContent>
    </Card>
  );
};

function FunnelController() {
  const [tray, setTray] = useState({
    cells: 6,
    cell_depth: 150,
    cell_width: 150,
  });

  //const status = useFunnelStore((state) => state.status);

  const [selectedCell, setSelectedCell] = useState(0);
  //const [funnelOffsets, setFunnelOffsets] = useState(
  //  defaultFunnelOffsets()
  //);

  //const fetchFunnelOffsets = async () => {
  //  const result = await getOffsets();
  //  setFunnelOffsets(result);
  //};

  //useEffect(() => {
  //  fetchFunnelOffsets();
  //}, []);

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Funnel Control</CardTitle>
      <CardContent className="px-0">
        <div className="flex items-stretch space-x-4">
          {/*<Conveyor />*/}
          <TrayConfigurator tray={tray} setTray={setTray} />
          <Controls tray={tray} idx={selectedCell} />
        </div>
        <div className="flex flex-col items-center justify-center pt-8">
          <span className="text-lg font-semibold">Tray Layout</span>
          <div className="flex space-x-2 w-[720px] h-72 items-end">
            {Array(tray.cells)
              .fill(0)
              .map((_r, cell_idx) => {
                return (
                  <div
                    style={{
                      width: `${
                        (720 - (tray.cells - 1) * 2) / tray.cells
                      }px`,
                      height: `${
                        64 * 0.25 * (tray.cell_width / 150)
                      }rem`,
                    }}
                    className={`block h-32 border border-slate-100 ${
                      cell_idx === selectedCell
                        ? 'bg-slate-300'
                        : 'bg-slate-50'
                    } rounded`}
                    onClick={async () => {
                      setSelectedCell(cell_idx);
                      const response = await funnelCommand({
                        command: 'MOVE_TO_CELL',
                        payload: {
                          tray: tray,
                          idx: cell_idx,
                        },
                      });
                      console.log(response);
                    }}
                  ></div>
                );
              })}
          </div>
        </div>
      </CardContent>
      {/*
      <div className="flex items-stretch space-x-8">

        <FunnelOffsetsForm
          key={JSON.stringify(funnelOffsets)}
          offsets={funnelOffsets}
          handleUpdate={async (value: FunnelOffsetsSchema) => {
            await updateOffsets(value);
            await fetchFunnelOffsets();
          }}
        />
      </div>
      */}
      {/*
      <Card className="grow">
        <CardHeader>
          <CardTitle>Funnel Positions</CardTitle>
          <CardDescription>
            Raw Funnel Encoder Position
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex space-x-8">
            <p>
              <b className="pr-2">Inlet Funnel:</b>
              {`${status.inlet_funnel}`}
            </p>
            <p>
              <b className="pr-2">Inlet Funnel Width:</b>
              {`${status.inlet_funnel_width}`}
            </p>
            <p>
              <b className="pr-2">Opposing Funnel:</b>
              {`${status.opposing_funnel}`}
            </p>
            <p>
              <b className="pr-2">Opposing Funnel Width:</b>
              {`${status.opposing_funnel_width}`}
            </p>
          </div>
        </CardContent>
      </Card>
        */}
    </Card>
  );
}

export default FunnelController;
