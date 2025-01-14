import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  useQueries,
} from '@tanstack/react-query';
import { getFreeCells, getItems } from '@/api';

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
    ],
    combine: (results) => {
      return {
        data: results.map((result) => result.data),
        pending: results.some((result) => result.isPending),
        error: results.some((result) => result.isError),
      };
    },
  });

  if (pending) {
    return <span>Loading...</span>;
  }

  if (error) {
    return <span>Error Fetching Inventory</span>;
  }
  console.log(data)

  return (
    <div className="flex flex-col w-full">
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
