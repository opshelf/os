import { useStateStore } from "@/store";
import {
  BarcodeIcon,
  CircleSlashIcon,
  PowerIcon,
  WeightIcon,
} from "lucide-react";
import { RobotController } from "@/domain/robot";
import { robotCommand } from "@/api/robot";
import { Button } from "../ui/button";
import StatusIndicator from "@/components/StatusIndicator";
import CommandDisplay from "@/components/CommandDisplay";
import QueuePanel from "@/components/QueuePanel";
import ErrorDrawer from "@/components/ErrorDrawer";

const Footer = () => {
  const { reading, scan, robotStatus } = useStateStore();

  const robot = new RobotController(robotCommand);
  const isConnected = robotStatus !== null;
  const isRunning = robotStatus?.running ?? false;
  const currentCommand = robotStatus?.current_command ?? null;

  return (
    <div className="flex w-full fixed inset-x-0 bottom-0 bg-white p-4 shadow-lg z-50 items-center">
      {/* Left side - Status information */}
      <div className="flex items-center gap-4">
        <StatusIndicator connected={isConnected} />
        <div className="h-4 w-px bg-gray-300" />
        <CommandDisplay command={currentCommand} isRunning={isRunning} />
        <QueuePanel />
        <div className="h-4 w-px bg-gray-300" />
        <ErrorDrawer />
      </div>

      {/* Center - Scan information */}
      <div className="flex-1 flex justify-center items-center space-x-2">
        <span className="leading-none text-sm">
          {scan !== null ? scan : "No Scan Detected"}
        </span>
        <BarcodeIcon className="h-4 w-4" />
      </div>

      {/* Right side - Scale controls */}
      <div className="flex items-center space-x-2">
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            await robot.tareScale();
          }}
        >
          <CircleSlashIcon className="h-4 w-4" />
          Tare
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            await robot.scalePower();
          }}
        >
          <PowerIcon className="h-4 w-4" />
          Power
        </Button>
        <span className="leading-none text-sm">
          {reading !== null ? `${reading.weight}g` : "Scale Not Connected"}
        </span>
        <WeightIcon className="h-4 w-4" />
      </div>
    </div>
  );
};

export default Footer;
