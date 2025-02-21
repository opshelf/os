import { useStateStore } from '@/store';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CrosshairIcon,
  HomeIcon,
  LoaderIcon,
  StopCircleIcon,
  RulerIcon,
} from 'lucide-react';
import { RobotController } from '@/domain/robot';
import { robotCommand } from '@/api/robot';
import { Button } from '../ui/button';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import z from 'zod';
import { useState } from 'react';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { AxisStatus } from '@/domain/status';

const AxisStatusDisplay = ({
  axisStatus,
  axisName,
  axisId,
}: {
  axisStatus: AxisStatus;
  axisName: string;
  axisId: number;
}) => {
  return (
    <div className="flex w-full px-2 items-center space-x-2">
      <span className="text-lg font-semibold w-32 text-left">
        {axisName}
      </span>
      <span className="text-md font-semibold w-32 text-left">
        Id: {axisId}
      </span>
      <div className="flex items-center">
        <div className="w-4 h-4 border px-1 py-1 bg-red-100 mr-2" />
        <p>Safety On</p>
      </div>
      <div className="flex items-center">
        <div className="w-4 h-4 border px-1 py-1 bg-red-100 mr-2" />
        <p>Error</p>
      </div>
      <div className="flex items-center">
        <RulerIcon className="mr-2" />
        <p>{axisStatus.position}</p>
      </div>
    </div>
  );
};

const MoveForm = ({ axis_id }: { axis_id: number }) => {
  const robot = new RobotController(robotCommand);
  const form = useForm<{ position: number }>({
    resolver: zodResolver(z.object({ position: z.number().min(0) })),
    defaultValues: {
      position: 0,
    },
  });

  async function onSubmit(values: { position: number }) {
    await robot.lowLevelAxisCommand({
      axis_id,
      command: {
        command: 'move_absolute',
        position: values.position,
      },
    });
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className="flex space-x-4 items-center"
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
        <Button type="submit">
          <CrosshairIcon />
          Move Absolute
        </Button>
      </form>
    </Form>
  );
};

const AxisControl = ({ axis_id }: { axis_id: number }) => {
  const robot = new RobotController(robotCommand);
  const [jog, setJog] = useState<number>(5);

  return (
    <div className="flex items-end space-x-2">
      <Button
        type="button"
        onClick={async () =>
          await robot.lowLevelAxisCommand({
            axis_id,
            command: {
              command: 'home',
            },
          })
        }
      >
        <HomeIcon /> Home
      </Button>
      <Button
        type="button"
        onClick={async () =>
          await robot.lowLevelAxisCommand({
            axis_id,
            command: {
              command: 'stop',
            },
          })
        }
      >
        <StopCircleIcon /> Stop
      </Button>
      <Button
        type="button"
        onClick={async () =>
          await robot.lowLevelAxisCommand({
            axis_id,
            command: {
              command: 'initialize',
            },
          })
        }
      >
        <LoaderIcon /> Initialize
      </Button>
      <div className="flex flex-col items-center">
        <p className="text-md">Move Relative</p>
        <div className="flex space-x-2">
          <Button
            type="button"
            className=""
            onClick={async () =>
              await robot.lowLevelAxisCommand({
                axis_id,
                command: {
                  command: 'move_relative',
                  position: -jog,
                },
              })
            }
          >
            <ArrowLeftIcon />
          </Button>
          <Select
            onValueChange={(value) => setJog(parseInt(value))}
            defaultValue={`${jog}`}
          >
            <SelectTrigger className="w-24">
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

          <Button
            type="button"
            className=""
            onClick={async () =>
              await robot.lowLevelAxisCommand({
                axis_id,
                command: {
                  command: 'move_relative',
                  position: jog,
                },
              })
            }
          >
            <ArrowRightIcon />
          </Button>
        </div>
      </div>

      <MoveForm axis_id={axis_id} />
    </div>
  );
};

//axes: Dict[int, TrinamicAxis] = {
//  100: self._gantry.x_axis,
//  101: self._gantry.y_axis,
//  102: self._tool.carriage,
//  110: self._funnel.inlet_funnel,
//  111: self._funnel.opposing_funnel,
//  112: self._funnel.slot_1,
//  113: self._funnel.slot_2,
//  114: self._funnel.slot_3,
//  137: self._workstation.output_box_1.door,
//  138: self._workstation.output_box_2.door,
//  139: self._workstation.output_box_3.door,
//}

const AxisController = () => {
  const { robotStatus } = useStateStore();

  if (robotStatus?.robot_status == undefined) {
    return (
      <div className="flex flex-col w-full space-y-4">
        Robot Not Connected
      </div>
    );
  } else {
    const axes: {
      [key: string]: { axisName: string; status: AxisStatus };
    } = {
      '100': {
        axisName: 'X Axis',
        status: robotStatus.robot_status.gantry_status.x_axis,
      },
      '101': {
        axisName: 'Y Axis',
        status: robotStatus.robot_status.gantry_status.y_axis,
      },
      '102': {
        axisName: 'Carriage',
        status: robotStatus.robot_status.tool_status.carriage_status,
      },
      '110': {
        axisName: 'Inlet Funnel',
        status: robotStatus.robot_status.funnel_status.inlet_funnel,
      },
      '111': {
        axisName: 'Opposing Funnel',
        status:
          robotStatus.robot_status.funnel_status.opposing_funnel,
      },
      '112': {
        axisName: 'Slot 1',
        status: robotStatus.robot_status.funnel_status.slot_1,
      },
      '113': {
        axisName: 'Slot 2',
        status: robotStatus.robot_status.funnel_status.slot_2,
      },
      '114': {
        axisName: 'Slot 3',
        status: robotStatus.robot_status.funnel_status.slot_3,
      },
      '137': {
        axisName: 'Output Box 1',
        status:
          robotStatus.robot_status.workstation_status.output_box_1
            .door_status,
      },
      '138': {
        axisName: 'Output Box 2',
        status:
          robotStatus.robot_status.workstation_status.output_box_2
            .door_status,
      },
      '139': {
        axisName: 'Output Box 3',
        status:
          robotStatus.robot_status.workstation_status.output_box_3
            .door_status,
      },
    };
    return (
      <div className="flex flex-col w-full space-y-4">
        <Accordion type="multiple">
          {Object.keys(axes).map((axisId) => {
            return (
              <AccordionItem
                value={`axis-${axisId}`}
                key={`axis-${axisId}`}
              >
                <AccordionTrigger>
                  <AxisStatusDisplay
                    axisId={parseInt(axisId)}
                    axisName={axes[axisId].axisName}
                    axisStatus={axes[axisId].status}
                  />
                </AccordionTrigger>
                <AccordionContent>
                  <AxisControl axis_id={parseInt(axisId)} />
                </AccordionContent>
              </AccordionItem>
            );
          })}
        </Accordion>
      </div>
    );
  }
};

export default AxisController;
