import { Card, CardContent, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ArrowLeft, ArrowRight, ArrowDown, ArrowUp } from "lucide-react";
import { useStateStore } from "@/store";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useState } from "react";
import { Label } from "@/components/ui/label";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { gantryCoordinateSchema, GantryCoordinateSchema } from "@/domain";
import { RobotController } from "@/domain/robot";
import { robotCommand } from "@/api/robot";

const MoveForm = () => {
  const form = useForm<GantryCoordinateSchema>({
    resolver: zodResolver(gantryCoordinateSchema),
    defaultValues: {
      x: 0,
      y: 0,
    },
  });

  const robot = new RobotController(robotCommand);

  async function onSubmit(values: GantryCoordinateSchema) {
    await robot.move(values);
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className="flex-col space-y-4 items-center"
      >
        <FormField
          control={form.control}
          name="x"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormLabel>X: </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`x`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="y"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormLabel>Y: </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`y`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <Button type="submit" className="w-full">
          MOVE
        </Button>
      </form>
    </Form>
  );
};

const GantryController = () => {
  const [jog, setJog] = useState<{
    x_jog: number;
    y_jog: number;
  }>({
    x_jog: 5,
    y_jog: 5,
  });

  const robot = new RobotController(robotCommand);

  const { robotStatus } = useStateStore();

  const gantryStatus = robotStatus?.robot_status?.gantry;

  const runJogCommand = async (axis: "x" | "y", movement: number) => {
    let defaultMove = {
      x: 0,
      y: 0,
    };
    if (axis == "x") {
      defaultMove[axis] = movement;
    }
    if (axis == "y") {
      defaultMove[axis] = movement;
    }
    await robot.gantry.gantryMoveRelativeCommand(defaultMove);
  };

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Gantry Control</CardTitle>
      <CardContent className="px-0 relative">
        <div className="flex space-x-8 w-full">
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <Button
              onClick={async () => {
                await robot.gantry.gantryHomeCommand();
              }}
            >
              HOME
            </Button>
            <Button
              onClick={async () => {
                await robot.gantry.gantryInitializeCommand();
              }}
            >
              INITIALIZE
            </Button>
            <Button
              onClick={async () => {
                await robot.gantry.gantryStopCommand();
              }}
            >
              STOP
            </Button>
          </div>
          <div className="flex flex-col space-y-1">
            <span className="text-xl w-full text-center">Jog Units</span>
            <div className="flex gap-2 items-center">
              <Label>X</Label>
              <Select
                onValueChange={(value) =>
                  setJog({ ...jog, x_jog: parseInt(value) })
                }
                defaultValue={`${jog.x_jog}`}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={`${jog.x_jog}mm`}
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
            <div className="flex gap-2 items-center">
              <Label>Y</Label>
              <Select
                onValueChange={(value) =>
                  setJog({ ...jog, y_jog: parseInt(value) })
                }
                defaultValue={`${jog.y_jog}`}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={`${jog.y_jog}mm`}
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
          </div>
          <div className="flex flex-col space-y-1 items-center">
            <span className="text-xl w-full">XY Jog</span>
            <div className="flex items-center justify-center">
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand("y", jog.y_jog)}
              >
                <ArrowUp />
              </Button>
            </div>
            <div className="flex items-center justify-between space-x-8">
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand("x", -jog.x_jog)}
              >
                <ArrowLeft />
              </Button>
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand("x", jog.x_jog)}
              >
                <ArrowRight />
              </Button>
            </div>
            <div className="flex items-center justify-center">
              <Button
                type="button"
                className=""
                onClick={() => runJogCommand("y", -jog.y_jog)}
              >
                <ArrowDown />
              </Button>
            </div>
          </div>
          <div className="flex flex-col items-center space-y-2 items-stretch">
            <MoveForm />
          </div>
        </div>
        {gantryStatus !== undefined && (
          <div className="flex w-full space-x-4 absolute bottom-0">
            <p>X Position: {gantryStatus.x_axis.position}</p>
            <p>Y Position: {gantryStatus.y_axis.position}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
};

export default GantryController;
