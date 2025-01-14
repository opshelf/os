import { Card, CardContent, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { RobotController } from '@/domain/robot';
import { robotCommand } from '@/api/robot';

const WorkstationController = () => {
  const robot = new RobotController(robotCommand);

  return (
    <Card className="flex flex-col px-4 py-4 space-y-4 w-fit">
      <CardTitle>Workstation</CardTitle>
      <CardContent className="px-0">
        <div className="flex items-center space-x-2 items-stretch">
          <Button
            onClick={async () => {
              await robot.workstation.workstationHomeOutputCommand();
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
              await robot.workstation.workstationOpenOutputCommand();
            }}
          >
            OPEN
          </Button>
          <Button
            onClick={async () => {
              await robot.workstation.workstationCloseOutputCommand();
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
