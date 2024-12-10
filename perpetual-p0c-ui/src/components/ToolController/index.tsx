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
import { toolCommand } from '@/api/tool';

const MoveForm = () => {
  const form = useForm<{ position: number }>({
    resolver: zodResolver(z.object({ position: z.number().min(0) })),
    defaultValues: {
      position: 0,
    },
  });

  async function onSubmit(values: { position: number }) {
    await toolCommand({ command: 'MOVE_Z', payload: values });
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
    false,
    false,
  ]);
  const [carriageSolenoids, setCarriageSolenoids] = useState<
    boolean[]
  >([false, false, false]);
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

    await toolCommand(defaultMove);
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
                await toolCommand({
                  command: 'HOME',
                  payload: null,
                });
              }}
            >
              HOME
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
                  command: 'INITIALIZE',
                  payload: null,
                });
              }}
            >
              INITIALIZE
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
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
                await toolCommand({
                  command: 'EXTEND_ACTUATORS',
                  payload: null,
                });
              }}
            >
              EXTEND
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
                  command: 'RETRACT_ACTUATORS',
                  payload: null,
                });
              }}
            >
              RETRACT
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
                  command: 'EXTEND_PULLER',
                  payload: null,
                });
              }}
            >
              EXTEND PULLER
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
                  command: 'RETRACT_PULLER',
                  payload: null,
                });
              }}
            >
              RETRACT PULLER
            </Button>
            <Button
              onClick={async () => {
                await toolCommand({
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
              <span className="text-lg font-semibold">
                CARRIAGE SOLENOIDS
              </span>
              <ToggleGroup type="single">
                {carriageSolenoids.map((value, idx) => {
                  return (
                    <ToggleGroupItem
                      value="underline"
                      aria-label="Toggle underline"
                      className="w-8 h-8 rounded-[50%] border"
                      data-state={value ? 'on' : 'off'}
                      onClick={async () => {
                        let newSolenoids = [...carriageSolenoids];
                        newSolenoids[idx] = !carriageSolenoids[idx];
                        setCarriageSolenoids(newSolenoids);
                        await toolCommand({
                          command: 'TURN_ON_CARRIAGE_SOLENOIDS',
                          payload: {
                            solenoids:
                              boolArrayToNumber(newSolenoids),
                          },
                        });
                      }}
                    />
                  );
                })}
              </ToggleGroup>
            </div>
            <div className="flex flex-col items-center space-y-2">
              <span className="text-lg font-semibold">
                PULLER SOLENOIDS
              </span>
              <ToggleGroup type="multiple">
                {slotSolenoids.map((value, idx) => {
                  return (
                    <ToggleGroupItem
                      value="bold"
                      aria-label="Toggle bold"
                      className="w-8 h-8 rounded-[50%] border"
                      data-state={value ? 'on' : 'off'}
                      onClick={async () => {
                        let newSolenoids = [...slotSolenoids];
                        newSolenoids[idx] = !slotSolenoids[idx];
                        setSlotSolenoids(newSolenoids);
                        await toolCommand({
                          command: 'TURN_ON_PULLER_SOLENOIDS',
                          payload: {
                            solenoids:
                              boolArrayToNumber(newSolenoids),
                          },
                        });
                      }}
                    />
                  );
                })}
              </ToggleGroup>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

export default ToolController;
