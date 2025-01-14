import { Label } from '../ui/label';
import { StorageModuleSide } from '@/domain';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectValue,
  SelectTrigger,
} from '../ui/select';
import { StorageModuleData } from '@/domain';
import { useEffect } from 'react';

type IntermediateData = {
  side: StorageModuleSide;
  shelf: number | null;
  tray: number | null;
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
  const { side, shelf, tray, cell } = locationData;

  // Utility to find the first available location
  const getFirstAvailableLocation = (side: StorageModuleSide) => {
    for (const [shelfIdx, shelf] of data[side].entries()) {
      for (const [trayIdx, tray] of shelf.trays.entries()) {
        for (const [slotIdx] of tray.slots.entries()) {
          return {
            side,
            shelf: shelfIdx,
            tray: trayIdx,
            cell: slotIdx,
          };
        }
      }
    }
    return { side, shelf: 0, tray: null, cell: null };
  };

  // Handle side change and update location
  const handleSideChange = (newSide: StorageModuleSide) => {
    if (data[newSide].length > 0) {
      const firstLocation = getFirstAvailableLocation(newSide);
      setLocationData(firstLocation);
    } else {
      setLocationData({
        side: newSide,
        shelf: null,
        tray: null,
        cell: null,
      });
    }
  };

  // Dynamic updates for dependent selections (shelf, tray, cell)
  useEffect(() => {
    if (shelf !== null && data[side][shelf]?.trays.length === 0) {
      //@ts-ignore
      setLocationData((prev: IntermediateData) => ({
        ...prev,
        tray: null,
        cell: null,
      })); //@ts-ignore
    }
    if (
      shelf !== null &&
      tray !== null &&
      data[side][shelf]?.trays[tray]?.slots.length === 0
    ) {
      //@ts-ignore
      setLocationData((prev: IntermediateData) => ({
        ...prev,
        cell: null,
      })); //@ts-ignore
    }
  }, [side, shelf, tray, data]);

  // Render helpers
  const renderShelfSelect = () => (
    <>
      <Label className="text-lg">Shelf</Label>
      <Select
        value={`${shelf}`}
        onValueChange={(value) =>
          setLocationData({
            ...locationData,
            shelf: parseInt(value),
            tray: null,
            cell: null,
          })
        }
      >
        <SelectTrigger className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {data[side].map((_, idx) => (
            <SelectItem value={`${idx}`} key={`shelf-${idx}`}>
              {`Shelf ${idx}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </>
  );

  const renderTraySelect = () => (
    <>
      <Label className="text-lg">Tray</Label>
      <Select
        value={`${tray}`}
        onValueChange={(value) =>
          setLocationData({
            ...locationData,
            tray: parseInt(value),
            cell: null,
          })
        }
      >
        <SelectTrigger className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {data[side][shelf!]?.trays.map((_, idx) => (
            <SelectItem value={`${idx}`} key={`tray-${idx}`}>
              {`Tray ${idx}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </>
  );

  const renderSlotSelect = () => (
    <>
      <Label className="text-lg">Slot</Label>
      <Select
        value={`${cell}`}
        onValueChange={(value) =>
          setLocationData({ ...locationData, cell: parseInt(value) })
        }
      >
        <SelectTrigger className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {data[side][shelf!]?.trays[tray!]?.slots.map((_, idx) => (
            <SelectItem value={`${idx}`} key={`slot-${idx}`}>
              {`Slot ${idx}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </>
  );

  return (
    <>
      <Label className="text-lg">Side</Label>
      <Select
        value={side}
        onValueChange={(value) =>
          handleSideChange(value as StorageModuleSide)
        }
      >
        <SelectTrigger className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="left_module">Left Module</SelectItem>
          <SelectItem value="right_module">Right Module</SelectItem>
        </SelectContent>
      </Select>

      {data[side].length > 0 && (
        <>
          {renderShelfSelect()}
          {shelf !== null &&
            data[side][shelf]?.trays.length > 0 &&
            renderTraySelect()}
          {shelf !== null &&
            tray !== null &&
            data[side][shelf]?.trays[tray]?.slots.length > 0 &&
            renderSlotSelect()}
        </>
      )}
    </>
  );
};
/*

export const CellSelectionForm = ({
  data,
}: {
  data: StorageModuleData;
}) => {
  const [locationData, setLocationData] = useState<{
    side: StorageModuleSide;
    shelf: number | null;
    tray: number | null;
    cell: number | null;
  }>({
    side: 'right_module',
    shelf: null,
    tray: null,
    cell: null,
  });

  const { side, shelf, tray, cell } = locationData;

  return (
    <>
      <Label className="text-lg">Side</Label>
      <Select
        value={side}
        onValueChange={(value) => {
          if (data[value as StorageModuleSide].length > 0) {
            let updates: {
              side: StorageModuleSide;
              shelf: number | null;
              tray: number | null;
              cell: number | null;
            } = {
              side: value as StorageModuleSide,
              shelf: 0,
              tray: null,
              cell: null,
            };
            let firstLocation: null | {
              side: StorageModuleSide;
              shelf: number | null;
              tray: number | null;
              cell: number | null;
            } = null;
            data[value as StorageModuleSide].forEach(
              (shelf, shelfIdx) => {
                shelf.trays.forEach((tray, trayIdx) => {
                  tray.slots.forEach((_, slotIdx) => {
                    if (firstLocation !== null) {
                    } else {
                      firstLocation = {
                        ...updates,
                        shelf: shelfIdx,
                        tray: trayIdx,
                        cell: slotIdx,
                      };
                    }
                  });
                });
              }
            );
            if (firstLocation !== null) {
              console.log(firstLocation);
              setLocationData(firstLocation);
            } else {
              setLocationData(updates);
            }
            //if (data[value as StorageModuleSide][0].trays.length >= 0)
          } else {
            setLocationData({
              side: value as StorageModuleSide,
              shelf: null,
              tray: null,
              cell: null,
            });
          }
        }}
      >
        <SelectTrigger className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="left_module">Left Module</SelectItem>
          <SelectItem value="right_module">Right Module</SelectItem>
        </SelectContent>
      </Select>
      {data[side].length > 0 && (
        <>
          <Label className="text-lg">Shelf</Label>
          <Select
            value={`${shelf}`}
            onValueChange={(value) =>
              setLocationData({
                ...locationData,
                shelf: parseInt(value),
              })
            }
          >
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {data[side].map((_, shelf_idx) => {
                return (
                  <SelectItem
                    value={`${shelf_idx}`}
                    key={`${shelf_idx}-pull-shelf`}
                  >
                    {`${shelf_idx}`}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
          {shelf !== null && data[side][shelf].trays.length > 0 && (
            <>
              <Label className="text-lg">Tray</Label>
              <Select
                value={`${tray}`}
                onValueChange={(value) =>
                  setLocationData({
                    ...locationData,
                    tray: parseInt(value),
                  })
                }
              >
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {shelf &&
                    data[side][shelf].trays.map((_, trayIdx) => {
                      return (
                        <SelectItem
                          value={`${trayIdx}`}
                          key={`${trayIdx}-pull-tray`}
                        >
                          {`${trayIdx}`}
                        </SelectItem>
                      );
                    })}
                </SelectContent>
              </Select>
              {shelf !== null &&
                tray !== null &&
                data[side][shelf].trays[tray].slots.length > 0 && (
                  <>
                    <Label className="text-lg">Slot</Label>
                    <Select
                      value={`${cell}`}
                      onValueChange={(value) =>
                        setLocationData({
                          ...locationData,
                          cell: parseInt(value),
                        })
                      }
                    >
                      <SelectTrigger className="w-32">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {shelf &&
                          data[side][shelf].trays[tray].slots.map(
                            (_, slotIdx) => {
                              return (
                                <SelectItem
                                  value={`${slotIdx}`}
                                  key={`${slotIdx}-pull-slot`}
                                >
                                  {`${slotIdx}`}
                                </SelectItem>
                              );
                            }
                          )}
                      </SelectContent>
                    </Select>
                  </>
                )}
            </>
          )}
        </>
      )}
    </>
  );
};
*/
