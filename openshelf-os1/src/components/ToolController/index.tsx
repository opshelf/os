import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { Slider } from '@/components/ui/slider';
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
import z from 'zod';
import { RobotController } from '@/domain/robot';
import { robotCommand } from '@/api/robot';
import { useStateStore } from '@/store';

const MoveForm = () => {
  const robot = new RobotController(robotCommand);
  const form = useForm<{ position: number }>({
    resolver: zodResolver(z.object({ position: z.number().min(0) })),
    defaultValues: {
      position: 0,
    },
  });

  async function onSubmit(values: { position: number }) {
    await robot.tool.toolMoveGripperAbsolute(values.position);
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
  const robot = new RobotController(robotCommand);

  const [jog, setJog] = useState<number>(5);
  const [jawSize, setJawSize] = useState<number>(30);

  const runJogCommand = async (movement: number) => {
    await robot.tool.toolMoveGripperRelative(movement);
  };
  const { robotStatus } = useStateStore();
  const toolStatus = robotStatus?.robot_status?.tool_status;

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Tool Control</CardTitle>
      <CardContent className="px-0 relative">
        <div className="flex space-x-8 w-full">
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <Button
              onClick={async () => {
                await robot.tool.toolHomeCommand();
              }}
            >
              HOME
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolInitializeCommand();
              }}
            >
              INITIALIZE
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolStopCommand();
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
              Move (mm)
            </span>
            <MoveForm />
          </div>
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <Button
              onClick={async () => {
                await robot.tool.toolExtendGripperCommand();
              }}
            >
              EXTEND GRIPPER
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolRetractGripperCommand();
              }}
            >
              RETRACT GRIPPER
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolOpenGripper();
              }}
            >
              OPEN GRIPPER
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolCloseGripper();
              }}
            >
              CLOSE GRIPPER
            </Button>
            <Button
              onClick={async () => {
                await robot.tool.toolPrepareGripper();
              }}
            >
              PREPARE GRIPPER
            </Button>
          </div>
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <span>Set Jaw Size</span>
            <Slider
              value={[jawSize]}
              onValueChange={(value) => setJawSize(value[0])}
              max={40}
              min={20}
              step={1}
              onValueCommit={async (value) => {
                await robot.tool.toolSetJawSize(value[0]);
              }}
            />
            <span className="text-center">{jawSize}mm</span>
          </div>
        </div>
        <div className="flex w-full abosolute bottom-0">
          {toolStatus !== undefined && (
            <p>
              Tool Position: {toolStatus.carriage_status.position}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

export default ToolController;
