import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { RobotController } from '@/domain/robot';
import { robotCommand } from '@/api/robot';
import { useState } from 'react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const WorkstationController = () => {
  const robot = new RobotController(robotCommand);
  const [outputBoxId, setOutputBoxId] = useState<number>(0)
  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Workstation</CardTitle>
      <CardContent className="px-0">
        <div className="flex items-center space-x-2 items-stretch">
          <Select
            onValueChange={(value) => setOutputBoxId(parseInt(value))}
            defaultValue={`${outputBoxId}`}
          >
            <SelectTrigger>
              <SelectValue
                className="w-24"
              />
            </SelectTrigger>
            <SelectContent className="w-24">
              <SelectItem value={`${0}`}>ALL</SelectItem>
              <SelectItem value={`${1}`}>1</SelectItem>
              <SelectItem value={`${2}`}>2</SelectItem>
              <SelectItem value={`${3}`}>3</SelectItem>
            </SelectContent>
          </Select>
          <Button
            onClick={async () => {
              await robot.workstation.workstationHomeOutputCommand(outputBoxId);
            }}
          >
            HOME
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationInitializeCommand();
            }}
          >
            INITIALIZE
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationStopCommand();
            }}
          >
            STOP
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationOpenOutputCommand(outputBoxId);
            }}
          >
            OPEN
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationCloseOutputCommand(outputBoxId);
            }}
          >
            CLOSE
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationStartOutputCommand();
            }}
          >
            START OUTPUT
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationStopOutputCommand();
            }}
          >
            STOP OUTPUT
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationStartInputCommand();
            }}
          >
            START INPUT
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationStopInputCommand();
            }}
          >
            STOP INPUT
          </Button>
        </div>
      </CardContent>
    </Card>
  );
};

export default WorkstationController;
