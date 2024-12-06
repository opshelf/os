import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useState } from 'react';
import { Label } from '@/components/ui/label';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group';
import z from 'zod';
import axios from 'axios';

const HOST = import.meta.env.DEV
  ? 'http://localhost:8000/api'
  : '/api';

axios.defaults.withCredentials = false;

const toolSubsytemCommands = z.enum([
  'HOME',
  'INITIALIZE',
  'MOVE_Z',
  'MOVE_Z_RELATIVE',
  'TURN_ON_PULLER_SOLENOIDS',
  'TURN_OFF_PULLER_SOLENOIDS',
  'TURN_ON_SLOT_SOLENOIDS',
  'TURN_OFF_SLOT_SOLENOIDS',
  'RETRACT_TRAY',
  'EXTEND_PULLER',
  'RETRACT_PULLER',
  'EXTEND_ACTUATORS',
  'RETRACT_ACTUATORS',
]);

export type ToolSubsytemCommands = z.infer<
  typeof toolSubsytemCommands
>;

const toolCommands = z.object({
  command: toolSubsytemCommands,
  payload: z.any(),
});

export type ToolCommands = z.infer<typeof toolCommands>;

export const executeToolCommand = async (command: any) => {
  return axios.post(`${HOST}/command`, {
    command: 'TOOL_COMMAND',
    payload: command,
  });
};

const MoveForm = () => {
  const form = useForm<{ position: number }>({
    resolver: zodResolver(z.object({ position: z.number().min(0) })),
    defaultValues: {
      position: 0,
    },
  });

  async function onSubmit(values: { position: number }) {
    await executeToolCommand({ command: 'MOVE_Z', payload: values });
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className="flex-col space-y-4 items-center"
      >
        <FormField
          control={form.control}
          name="position"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`position`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <Button type="submit">MOVE</Button>
      </form>
    </Form>
  );
};

const ToolController = () => {
  const [jog, setJog] = useState<number>(5);
  const [slotSolenoids, setSlotSolenoids] = useState<boolean[]>([
    false,
    false,
    false,
    false,
  ]);
  const [pullerSolenoids, setPullerSolenoids] = useState<number>(0);
  const runJogCommand = async (movement: number) => {
    let defaultMove: {
      command: 'MOVE_Z_RELATIVE';
      payload: { position: number };
    } = {
      command: 'MOVE_Z_RELATIVE',
      payload: {
        position: movement,
      },
    };

    await executeToolCommand(defaultMove);
  };

  const boolArrayToNumber = (arr: boolean[]): number => {
    let out: number = 0;
    for (const [idx, bit] of arr.entries()) {
      out = out | ((bit ? 1 : 0) << idx);
    }
    return out;
  };

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Tool Control</CardTitle>
      <CardContent className="px-0">
        <div className="flex space-x-8 w-full">
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'HOME',
                  payload: null,
                });
              }}
            >
              HOME
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'INITIALIZE',
                  payload: null,
                });
              }}
            >
              INITIALIZE
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'STOP',
                  payload: null,
                });
              }}
            >
              STOP
            </Button>
          </div>
          <div className="flex flex-col space-y-2">
            <span className="text-xl w-full text-center">
              Jog Units
            </span>
            <div className="flex gap-2 items-center">
              <Label>Z</Label>
              <Select
                onValueChange={(value) => setJog(parseInt(value))}
                defaultValue={`${jog}`}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={`${jog}mm`}
                    className="w-24"
                  />
                </SelectTrigger>
                <SelectContent className="w-24">
                  <SelectItem value={`${1}`}>1mm</SelectItem>
                  <SelectItem value={`${5}`}>5mm</SelectItem>
                  <SelectItem value={`${10}`}>10mm</SelectItem>
                  <SelectItem value={`${25}`}>25mm</SelectItem>
                  <SelectItem value={`${100}`}>100mm</SelectItem>
                  <SelectItem value={`${250}`}>250mm</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center justify-between space-x-2">
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand(-jog)}
              >
                <ArrowLeft />
              </Button>
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand(jog)}
              >
                <ArrowRight />
              </Button>
            </div>
          </div>
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <span className="text-xl w-full text-center">
              Absolute Move (mm)
            </span>
            <MoveForm />
          </div>
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'EXTEND_ACTUATORS',
                  payload: null,
                });
              }}
            >
              EXTEND
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'RETRACT_ACTUATORS',
                  payload: null,
                });
              }}
            >
              RETRACT
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'EXTEND_PULLER',
                  payload: null,
                });
              }}
            >
              EXTEND PULLER
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'RETRACT_PULLER',
                  payload: null,
                });
              }}
            >
              RETRACT PULLER
            </Button>
            <Button
              onClick={async () => {
                await executeToolCommand({
                  command: 'RETRACT_TRAY',
                  payload: null,
                });
              }}
            >
              RETRACT TRAY
            </Button>
          </div>

          <div className="flex flex-col items-center space-y-2">
            <div className="flex flex-col items-center space-y-2">
              <span>PULLER SOLENOIDS</span>
              <ToggleGroup type="single">
                <ToggleGroupItem
                  value="1"
                  aria-label="Toggle bold"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={pullerSolenoids >= 1 ? 'on' : 'off'}
                  onClick={async () => {
                    if (pullerSolenoids == 1) {
                      setPullerSolenoids(0);
                      await executeToolCommand({
                        command: 'TURN_OFF_PULLER_SOLENOIDS',
                        payload: null,
                      });
                    } else {
                      setPullerSolenoids(1);
                      await executeToolCommand({
                        command: 'TURN_ON_PULLER_SOLENOIDS',
                        payload: { solenoids: 1 },
                      });
                    }
                  }}
                ></ToggleGroupItem>
                <ToggleGroupItem
                  value="2"
                  aria-label="Toggle italic"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={pullerSolenoids >= 2 ? 'on' : 'off'}
                  onClick={async () => {
                    setPullerSolenoids(2);
                    await executeToolCommand({
                      command: 'TURN_ON_PULLER_SOLENOIDS',
                      payload: { solenoids: 2 },
                    });
                  }}
                ></ToggleGroupItem>
                <ToggleGroupItem
                  value="underline"
                  aria-label="Toggle underline"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={pullerSolenoids >= 3 ? 'on' : 'off'}
                  onClick={async () => {
                    await executeToolCommand({
                      command: 'TURN_ON_PULLER_SOLENOIDS',
                      payload: { solenoids: 3 },
                    });
                    setPullerSolenoids(3);
                  }}
                ></ToggleGroupItem>
              </ToggleGroup>
            </div>
            <div className="flex flex-col items-center space-y-2">
              <span>SLOT SOLENOIDS</span>
              <ToggleGroup type="multiple">
                <ToggleGroupItem
                  value="bold"
                  aria-label="Toggle bold"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={slotSolenoids[0] ? 'on' : 'off'}
                  onClick={async () => {
                    let newSolenoids = [...slotSolenoids];
                    newSolenoids[0] = !slotSolenoids[0];
                    setSlotSolenoids(newSolenoids);
                    await executeToolCommand({
                      command: 'TURN_ON_SLOT_SOLENOIDS',
                      payload: {
                        solenoids: boolArrayToNumber(newSolenoids),
                      },
                    });
                  }}
                ></ToggleGroupItem>
                <ToggleGroupItem
                  value="italic"
                  aria-label="Toggle italic"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={slotSolenoids[1] ? 'on' : 'off'}
                  onClick={async () => {
                    let newSolenoids = [...slotSolenoids];
                    newSolenoids[1] = !slotSolenoids[1];
                    setSlotSolenoids(newSolenoids);
                    await executeToolCommand({
                      command: 'TURN_ON_SLOT_SOLENOIDS',
                      payload: {
                        solenoids: boolArrayToNumber(newSolenoids),
                      },
                    });
                  }}
                ></ToggleGroupItem>
                <ToggleGroupItem
                  value="underline"
                  aria-label="Toggle underline"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={slotSolenoids[2] ? 'on' : 'off'}
                  onClick={async () => {
                    let newSolenoids = [...slotSolenoids];
                    newSolenoids[2] = !slotSolenoids[2];
                    setSlotSolenoids(newSolenoids);
                    await executeToolCommand({
                      command: 'TURN_ON_SLOT_SOLENOIDS',
                      payload: {
                        solenoids: boolArrayToNumber(newSolenoids),
                      },
                    });
                  }}
                ></ToggleGroupItem>
                <ToggleGroupItem
                  value="bold"
                  aria-label="Toggle bold"
                  className="w-8 h-8 rounded-[50%] border"
                  data-state={slotSolenoids[3] ? 'on' : 'off'}
                  onClick={async () => {
                    let newSolenoids = [...slotSolenoids];
                    newSolenoids[3] = !slotSolenoids[3];
                    setSlotSolenoids(newSolenoids);
                    await executeToolCommand({
                      command: 'TURN_ON_SLOT_SOLENOIDS',
                      payload: {
                        solenoids: boolArrayToNumber(newSolenoids),
                      },
                    });
                  }}
                ></ToggleGroupItem>
              </ToggleGroup>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

export default ToolController;
