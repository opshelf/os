import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectItem,
  SelectContent,
  SelectValue,
  SelectTrigger,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../ui/card";
import { storeCommand } from "@/api/robot";
import { print } from "@/api/print";

export default function DimensionForm() {
  const [itemType, setItemState] = useState<"bottle" | "box">("bottle");
  const [itemHeight, setItemHeight] = useState("50");
  const [itemWidth, setItemWidth] = useState("50");
  const [itemLength, setItemLength] = useState("50");

  // const save = async () => {
  //   if (itemType === "bottle") {
  //     const dimensions = {
  //       height: parseFloat(itemHeight),
  //       width: parseFloat(itemWidth),
  //       length: parseFloat(itemWidth),
  //     };
  //     await updateSizes(data, dimensions);
  //   } else {
  //     const dimensions = {
  //       height: parseFloat(itemHeight),
  //       width: parseFloat(itemWidth),
  //       length: parseFloat(itemLength),
  //     };
  //     await updateSizes(data, dimensions);
  //   }
  // };

  // const store = async () => {
  //   if (itemType === "bottle") {
  //     const dimensions = {
  //       height: parseFloat(itemHeight),
  //       width: parseFloat(itemWidth),
  //       length: parseFloat(itemWidth),
  //     };
  //     await updateSizes(data, dimensions);
  //     await storeCommand(data);
  //   } else {
  //     const dimensions = {
  //       height: parseFloat(itemHeight),
  //       width: parseFloat(itemWidth),
  //       length: parseFloat(itemLength),
  //     };
  //     await updateSizes(data, dimensions);
  //     await storeCommand(data);
  //   }
  // };

  const printLabel = async () => {
    const dimensions = {
      height: parseFloat(itemHeight),
      width: parseFloat(itemWidth),
      length: parseFloat(itemLength),
    };
    await print(dimensions, {});
  };

  //const checkItemSize = async () => {
  //  try {
  //    const size = await getSize(data);
  //    setItemHeight(size.height.toString());
  //    setItemWidth(size.width.toString());
  //    setItemLength(size.length.toString());
  //  } catch (err) {
  //    setItemHeight("");
  //    setItemWidth("");
  //    setItemLength("");
  //    console.log(err);
  //  }
  //};

  //useEffect(() => {
  //  checkItemSize();
  //}, [data]);

  return (
    <Card className="w-full">
      <CardHeader>
        <CardTitle>Object Dimensions</CardTitle>
        <CardDescription>
          <p>Enter the Scanned Objects Size or Edit it if Necessary</p>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex gap-4 items-end">
        <div className="flex flex-col gap-2">
          <Label>Item Type:</Label>
          <Select
            onValueChange={(value) => setItemState(value as "bottle" | "box")}
            value={itemType}
          >
            <SelectTrigger className="w-[180px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="bottle">Bottle</SelectItem>
              <SelectItem value="box">Box</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-2">
          <Label>Height:</Label>
          <Input
            className="w-[180px]"
            value={itemHeight}
            onChange={(e) => setItemHeight(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label>Width:</Label>
          <Input
            className="w-[180px]"
            value={itemWidth}
            onChange={(e) => setItemWidth(e.target.value)}
          />
        </div>
        {itemType === "box" && (
          <div className="flex flex-col gap-2">
            <Label>Length:</Label>
            <Input
              className="w-[180px]"
              value={itemLength}
              onChange={(e) => setItemLength(e.target.value)}
            />
          </div>
        )}

        <div className="flex space-x-4">
          <Button type="button" onClick={() => printLabel()}>
            Print Label
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
