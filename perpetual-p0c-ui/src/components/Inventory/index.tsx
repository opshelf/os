import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const Inventory = () => {
  return (
    <div>
      <Table>
        <TableCaption>A list of your recent invoices.</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead className="w-[100px]">Side</TableHead>
            <TableHead className="w-[100px]">Shelf</TableHead>
            <TableHead className="w-[100px]">Slot</TableHead>
            <TableHead className="w-[100px]">Cell</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className="font-medium">Right</TableCell>
            <TableCell>1</TableCell>
            <TableCell>1</TableCell>
            <TableCell>1</TableCell>
            <TableCell>Empty</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
};

export default Inventory;
