import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getSizes, updateSizes, deleteSizes, addItem } from "@/api/sizes";
import { printUpca } from "@/api/print";
import { useState } from "react";
import { PrinterIcon } from "lucide-react";

const Sizes = () => {
  const { data, error, isLoading } = useQuery({
    queryKey: ["sizes"],
    queryFn: getSizes,
  });

  const queryClient = useQueryClient();

  const [editUPC, setEditUPC] = useState<string | null>(null);
  const [addDialogOpen, setAddDialogOpen] = useState(false);

  const [formData, setFormData] = useState({
    length: 0,
    width: 0,
    height: 0,
    name: "",
  });

  const openEditDialog = (upc: string, item: any) => {
    setEditUPC(upc);
    setFormData({
      length: item.size.length,
      width: item.size.width,
      height: item.size.height,
      name: item.data?.name ?? "",
    });
  };

  const mutation = useMutation({
    mutationFn: async () => {
      if (!editUPC) return;
      await updateSizes({
        upca: editUPC,
        item: {
          size: {
            length: formData.length,
            width: formData.width,
            height: formData.height,
          },
          data: {
            name: formData.name,
          },
        },
      });
    },
    onSuccess: () => {
      setEditUPC(null);
      queryClient.invalidateQueries({ queryKey: ["sizes"] });
    },
  });

  const addMutation = useMutation({
    mutationFn: async () => {
      await addItem({
        size: {
          length: formData.length,
          width: formData.width,
          height: formData.height,
        },
        data: {
          name: formData.name,
        },
      });
    },
    onSuccess: () => {
      setAddDialogOpen(false);
      queryClient.invalidateQueries({ queryKey: ["sizes"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (upc: string) => {
      await deleteSizes(upc);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sizes"] });
    },
  });

  if (isLoading) return <span>Loading...</span>;
  if (error || !data) return <span>Error Fetching Sizes</span>;

  const items = Object.entries(data.items);

  return (
    <div className="flex flex-col w-full">
      <Table>
        <TableCaption>Size Database Items</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>UPC-A</TableHead>
            <TableHead>Length</TableHead>
            <TableHead>Width</TableHead>
            <TableHead>Height</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map(([upc, info]) => (
            <TableRow key={upc}>
              <TableCell className="font-mono">{upc}</TableCell>
              <TableCell>{info.size.length}</TableCell>
              <TableCell>{info.size.width}</TableCell>
              <TableCell>{info.size.height}</TableCell>
              <TableCell>{info.data?.name ?? <em>—</em>}</TableCell>
              <TableCell>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    onClick={() => openEditDialog(upc, info)}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={() => deleteMutation.mutate(upc)}
                  >
                    Delete
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={async () => await printUpca(upc)}
                  >
                    <PrinterIcon />
                    Print
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <div className="flex justify-end mt-4">
        <Button
          onClick={() => {
            setFormData({ length: 0, width: 0, height: 0, name: "" });
            setAddDialogOpen(true);
          }}
        >
          Add Item
        </Button>
      </div>

      {/* Edit Dialog */}
      <Dialog open={!!editUPC} onOpenChange={() => setEditUPC(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Item</DialogTitle>
            <DialogDescription>
              Update the dimensions and name for this item in the size database.
            </DialogDescription>
          </DialogHeader>
          <DimensionForm
            formData={formData}
            setFormData={setFormData}
            onSubmit={() => mutation.mutate()}
            onCancel={() => setEditUPC(null)}
          />
        </DialogContent>
      </Dialog>

      {/* Add Dialog */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Item</DialogTitle>
            <DialogDescription>
              Create a new item by entering its dimensions and name.
            </DialogDescription>
          </DialogHeader>
          <DimensionForm
            formData={formData}
            setFormData={setFormData}
            onSubmit={() => addMutation.mutate()}
            onCancel={() => setAddDialogOpen(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default Sizes;

// Shared dimension + name form
function DimensionForm({
  formData,
  setFormData,
  onSubmit,
  onCancel,
}: {
  formData: { length: number; width: number; height: number; name: string };
  setFormData: (data: typeof formData) => void;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="space-y-4">
      <div>
        <Label>Length</Label>
        <Input
          type="number"
          value={formData.length}
          onChange={(e) =>
            setFormData({ ...formData, length: +e.target.value })
          }
        />
      </div>
      <div>
        <Label>Width</Label>
        <Input
          type="number"
          value={formData.width}
          onChange={(e) => setFormData({ ...formData, width: +e.target.value })}
        />
      </div>
      <div>
        <Label>Height</Label>
        <Input
          type="number"
          value={formData.height}
          onChange={(e) =>
            setFormData({ ...formData, height: +e.target.value })
          }
        />
      </div>
      <div>
        <Label>Name</Label>
        <Input
          value={formData.name}
          onChange={(e) => setFormData({ ...formData, name: e.target.value })}
        />
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button onClick={onSubmit}>Save</Button>
      </div>
    </div>
  );
}
