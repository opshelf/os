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

const Footer = () => {
  const { reading, scan } = useStateStore();

  const robot = new RobotController(robotCommand);
  return (
    <div className="flex w-full fixed inset-x-0 bottom-0 bg-white p-4 shadow-lg z-50 justify-end">
      <div className="flex items-center space-x-2">
        <span className="leading-none">
          {scan !== null ? scan : "No Scan Detected"}
        </span>
        <BarcodeIcon />
      </div>
      <div className="flex grow-1" />
      <div className="flex items-center space-x-2">
        <Button
          onClick={async () => {
            await robot.tareScale();
          }}
        >
          <CircleSlashIcon />
          Tare Scale
        </Button>
        <Button
          onClick={async () => {
            await robot.scalePower();
          }}
        >
          <PowerIcon />
          Toggle Scale Power
        </Button>
        <span className="leading-none">
          {reading !== null ? reading.weight : "Scale Not Connected"}
        </span>
        <WeightIcon />
      </div>
    </div>
  );
};

export default Footer;
