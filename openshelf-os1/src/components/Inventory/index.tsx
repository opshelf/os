import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useStateStore } from "@/store"; // your zustand store
import { Button } from "../ui/button";
import { RobotController } from "@/domain/robot";
import { robotCommand } from "@/api/robot";
import { setStorage } from "@/api/robot";

const StorageCells = () => {
  const robotStatus = useStateStore((state) => state.robotStatus);

  const storage = robotStatus?.robot_status?.storage;

  if (!storage) {
    return <div>No storage data available</div>;
  }

  const extractNonEmptyCells = (moduleSide: "left_module" | "right_module") => {
    return storage[moduleSide].flatMap((shelf, shelfIdx) =>
      shelf.cells
        .map((cell, cellIdx) => ({
          cell,
          location: { side: moduleSide, shelfIdx, cellIdx },
        }))
        .filter(({ cell }) => cell.status !== "EMPTY" && !cell.empty),
    );
  };

  const nonEmptyCells = [
    ...extractNonEmptyCells("left_module"),
    ...extractNonEmptyCells("right_module"),
  ];

  const robot = new RobotController(robotCommand);

  return (
    <div className="flex flex-col w-full">
      <Table>
        <TableCaption>Non-Empty Storage Cells</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Side</TableHead>
            <TableHead>Shelf</TableHead>
            <TableHead>Cell</TableHead>
            <TableHead>UPC-A</TableHead>
            <TableHead>Length</TableHead>
            <TableHead>Width</TableHead>
            <TableHead>Height</TableHead>
            <TableHead>Weight</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {nonEmptyCells.map(({ cell, location }, index) => (
            <TableRow key={index}>
              <TableCell>{location.side}</TableCell>
              <TableCell>{location.shelfIdx}</TableCell>
              <TableCell>{location.cellIdx}</TableCell>
              <TableCell>{cell.item?.upca ?? <em>—</em>}</TableCell>
              <TableCell>
                {cell.item?.dimensions.length ?? <em>—</em>}
              </TableCell>
              <TableCell>{cell.item?.dimensions.width ?? <em>—</em>}</TableCell>
              <TableCell>
                {cell.item?.dimensions.height ?? <em>—</em>}
              </TableCell>
              <TableCell>{cell.item?.weight ?? <em>—</em>}</TableCell>
              <TableCell>
                <div className="flex gap-2">
                  <Button
                    onClick={async () => {
                      await robot.retrieveItem({
                        side: location.side,
                        shelf_idx: location.shelfIdx,
                        cell_idx: location.cellIdx,
                      });
                    }}
                  >
                    Retrieve
                  </Button>
                  <Button
                    variant={"destructive"}
                    onClick={async () => {
                      console.log(storage);
                      let newStorage = { ...storage };
                      newStorage[location.side][location.shelfIdx].cells[
                        location.cellIdx
                      ] = {
                        ...newStorage[location.side][location.shelfIdx].cells[
                          location.cellIdx
                        ],
                        status: "EMPTY",
                        item: null,
                      };
                      await setStorage(storage);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
};

export default StorageCells;
