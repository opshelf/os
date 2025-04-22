import { useStateStore } from "@/store";
import { BarcodeIcon, WeightIcon } from "lucide-react";

const Footer = () => {
  const { reading, scan } = useStateStore();
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
        <span className="leading-none">
          {reading !== null ? reading.weight : "Scale Not Connected"}
        </span>
        <WeightIcon />
      </div>
    </div>
  );
};

export default Footer;
