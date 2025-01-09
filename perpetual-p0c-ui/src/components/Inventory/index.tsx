import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '../ui/button';
import { useState } from 'react';
import {
  useQueries,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query';
import {
  addItem,
  getFreeCells,
  getItems,
  getStorage,
  removeItem,
} from '@/api';
import { CellLocationData, StorageModuleSide } from '@/domain';
import { createBaseMessage, robotCommand } from '@/api/robot';
import { v4 as uuidv4 } from 'uuid';
import { Switch } from '../ui/switch';
import { Label } from '../ui/label';
import {
  StorageModuleCellLocation,
  StorageModuleData,
} from '@/domain';
import { CellSelectionForm } from '../CellLocationForm';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { useStateStore } from '@/store';
import { GS1 } from '@/domain';
import DimensionForm from '../DimensionForm';

const InventoryTable = ({ data }: { data: CellLocationData[] }) => {
  return (
    <Table>
      <TableCaption>A list of your recent invoices.</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead className="w-[100px]">Side</TableHead>
          <TableHead className="w-[100px]">Shelf</TableHead>
          <TableHead className="w-[100px]">Tray</TableHead>
          <TableHead className="w-[100px]">Cell</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((data) => {
          return (
            <TableRow>
              <TableCell className="font-medium">
                {data.location.side}
              </TableCell>
              <TableCell>{data.location.shelf_idx}</TableCell>
              <TableCell>{data.location.tray_idx}</TableCell>
              <TableCell>{data.location.slot_idx}</TableCell>
              <TableCell className="space-x-2">
                <Button
                  onClick={async () => {
                    await robotCommand({
                      ...createBaseMessage(),
                      command: 'retrieve_item',
                      payload: {
                        location: data.location,
                        item: null,
                      },
                    });
                  }}
                >
                  Retrieve Item
                </Button>
                <Button
                  variant={'destructive'}
                  onClick={async () => {
                    await removeItem(data.location);
                  }}
                >
                  Remove Item
                </Button>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
};

const FreeCellsTable = ({
  cells,
}: {
  cells: StorageModuleCellLocation[];
}) => {
  return (
    <Table>
      <TableCaption>A list of your recent invoices.</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead className="w-[100px]">Side</TableHead>
          <TableHead className="w-[100px]">Shelf</TableHead>
          <TableHead className="w-[100px]">Tray</TableHead>
          <TableHead className="w-[100px]">Cell</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {cells.map((cell) => {
          return (
            <TableRow>
              <TableCell className="font-medium">
                {cell.side}
              </TableCell>
              <TableCell>{cell.shelf_idx}</TableCell>
              <TableCell>{cell.tray_idx}</TableCell>
              <TableCell>{cell.slot_idx}</TableCell>
              <TableCell>
                <Button>Store Item</Button>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
};

const AddInventoryForm = ({
  data,
  handleAdd,
}: {
  data: StorageModuleData;
  handleAdd: (location: StorageModuleCellLocation) => void;
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
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Item to Inventory </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <Label className="text-xl">Store in Location</Label>
        <div className="flex flex-col space-y-2">
          <CellSelectionForm
            data={data}
            setLocationData={setLocationData}
            locationData={locationData}
          />
          {locationData.shelf !== null &&
            locationData.tray !== null &&
            locationData.cell !== null && (
              <Button
                onClick={() => {
                  handleAdd({
                    side: locationData.side,
                    tray_idx: locationData.tray as number,
                    shelf_idx: locationData.shelf as number,
                    slot_idx: locationData.cell as number,
                  });
                }}
              >
                Add Item
              </Button>
            )}
        </div>
      </CardContent>
    </Card>
  );
};

const GS1Card = ({ gs1 }: { gs1: GS1 }) => {
  const { addGs1 } = useStateStore();
  return (
    <Card className="w-fit">
      <CardHeader>
        <CardTitle>GS1</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <ul className="space-y-2">
          <li>{`GTIN: ${gs1.gtin}`}</li>
          <li>{`EXPIRY: ${gs1.expiry}`}</li>
          <li>{`LOT: ${gs1.lot}`}</li>
          <li>{`SN: ${gs1.sn}`}</li>
        </ul>
        <Button onClick={() => addGs1(null)}>Clear</Button>
      </CardContent>
    </Card>
  );
};

const ScannerAddForm = () => {
  const { gs1 } = useStateStore();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Item to Inventory </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <Label className="text-xl">Store in Location</Label>
        <div className="flex space-x-2">
          {gs1 !== null && (
            <>
              <GS1Card gs1={gs1} />
              <DimensionForm data={gs1} />
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
};

const Inventory = () => {
  const { data, error, pending } = useQueries({
    queries: [
      {
        queryKey: ['items'],
        queryFn: getItems,
      },
      {
        queryKey: ['free_cells'],
        queryFn: getFreeCells,
      },
      {
        queryKey: ['storage'],
        queryFn: getStorage,
      },
    ],
    combine: (results) => {
      return {
        data: results.map((result) => result.data),
        pending: results.some((result) => result.isPending),
        error: results.some((result) => result.isError),
      };
    },
  });

  const queryClient = useQueryClient();

  const addMutation = useMutation({
    mutationFn: addItem,
    onSuccess: () => {
      // Invalidate and refetch
      queryClient.invalidateQueries({
        queryKey: ['items', 'free_cells', 'storage'],
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: removeItem,
    onSuccess: () => {
      // Invalidate and refetch
      queryClient.invalidateQueries({
        queryKey: ['items', 'free_cells', 'storage'],
      });
    },
  });

  const [showFreeCells, setShowFreeCells] = useState<boolean>(false);

  if (pending) {
    return <span>Loading...</span>;
  }

  if (error) {
    return <span>Error Fetching Inventory</span>;
  }

  return (
    <div className="flex flex-col w-full">
      <div className="flex space-x-4 items-center">
        <div className="grow" />
        <Label>Show Free Cells</Label>
        <Switch
          checked={showFreeCells}
          onCheckedChange={setShowFreeCells}
        />
      </div>
      {showFreeCells ? (
        <FreeCellsTable
          cells={data[1] as StorageModuleCellLocation[]}
        />
      ) : (
        <>
          <InventoryTable data={data[0] as CellLocationData[]} />
          <AddInventoryForm
            data={data[2] as StorageModuleData}
            handleAdd={(location: StorageModuleCellLocation) => {
              addMutation.mutate({ location, item: null });
            }}
          />
          <ScannerAddForm />
        </>
      )}
    </div>
  );
};

export default Inventory;
