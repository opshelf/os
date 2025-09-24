import { Label } from "../ui/label";
import { StorageModuleSide, StorageModuleData } from "@/domain";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectValue,
  SelectTrigger,
} from "../ui/select";
import { useEffect } from "react";

export type IntermediateData = {
  side: StorageModuleSide;
  shelf: number | null;
  cell: number | null;
};

export const CellSelectionForm = ({
  data,
  locationData,
  setLocationData,
}: {
  data: StorageModuleData;
  locationData: IntermediateData;
  setLocationData: (location: IntermediateData) => void;
}) => {
  const { side, shelf, cell } = locationData;

  // When side changes, pick first shelf & cell if available
  const handleSideChange = (newSide: StorageModuleSide) => {
    if (data[newSide].length > 0) {
      const firstShelf = 0;
      const firstCell = data[newSide][firstShelf].cells.length > 0 ? 0 : null;
      setLocationData({ side: newSide, shelf: firstShelf, cell: firstCell });
    } else {
      setLocationData({ side: newSide, shelf: null, cell: null });
    }
  };

  // If side changes to something with no shelves (or shelf out‑of‑range), reset shelf & cell
  useEffect(() => {
    const shelves = data[side];
    if (shelves.length === 0) {
      setLocationData({ side, shelf: null, cell: null });
    } else if (shelf === null || shelf >= shelves.length) {
      const newShelf = 0;
      const newCell = shelves[newShelf].cells.length > 0 ? 0 : null;
      setLocationData({ side, shelf: newShelf, cell: newCell });
    }
  }, [side]);

  // If shelf changes, reset cell if out‑of‑range or empty
  useEffect(() => {
    if (shelf !== null) {
      const cells = data[side][shelf].cells;
      if (cells.length === 0) {
        setLocationData((prev) => ({ ...prev, cell: null }));
      } else if (cell === null || cell >= cells.length) {
        setLocationData((prev) => ({ ...prev, cell: 0 }));
      }
    }
  }, [side, shelf]);

  const renderShelfSelect = () => (
    <>
      <Label className="text-lg">Shelf</Label>
      <Select
        value={shelf !== null ? `${shelf}` : ""}
        onValueChange={(value) =>
          setLocationData({
            side,
            shelf: parseInt(value, 10),
            cell: null, // will be set by the shelf‑useEffect
          })
        }
      >
        <SelectTrigger className="w-48">
          <SelectValue placeholder="Select shelf" />
        </SelectTrigger>
        <SelectContent>
          {data[side].map((_, idx) => (
            <SelectItem value={`${idx}`} key={idx}>
              {`Shelf ${idx}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </>
  );

  const renderCellSelect = () => (
    <>
      <Label className="text-lg">Cell</Label>
      <Select
        value={cell !== null ? `${cell}` : ""}
        onValueChange={(value) =>
          setLocationData((prev) => ({
            ...prev,
            cell: parseInt(value, 10),
          }))
        }
      >
        <SelectTrigger className="w-48">
          <SelectValue placeholder="Select cell" />
        </SelectTrigger>
        <SelectContent>
          {data[side][shelf!].cells.map((_, idx) => (
            <SelectItem value={`${idx}`} key={idx}>
              {`Cell ${idx}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </>
  );

  return (
    <div className="space-y-1">
      <div>
        <Label className="text-lg">Side</Label>
        <Select
          value={side}
          onValueChange={(v) => handleSideChange(v as StorageModuleSide)}
        >
          <SelectTrigger className="w-48">
            <SelectValue placeholder="Select side" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="left_module">Left Module</SelectItem>
            <SelectItem value="right_module">Right Module</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {data[side].length > 0 && renderShelfSelect()}

      {shelf !== null &&
        data[side][shelf].cells.length > 0 &&
        renderCellSelect()}
    </div>
  );
};
