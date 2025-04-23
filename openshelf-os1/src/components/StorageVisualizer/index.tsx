import { StorageModuleData } from "@/domain";
import { useState } from "react";

const StorageVisualizer = ({ data }: { data: StorageModuleData }) => {
  const [side, setSide] = useState<keyof StorageModuleData>("left_module");
  return <div className="flex flex-col"></div>;
};
