import { useQuery } from '@tanstack/react-query';
import { getCommands } from '@/api/robot';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '../ui/card';

const CommandList = () => {
  const { isPending, isError, data, error } = useQuery({
    queryKey: ['commands'],
    queryFn: getCommands,
  });

  if (isPending) {
    return <span>Loading...</span>;
  }

  if (isError) {
    return <span>Error: {error.message}</span>;
  }

  return (
    <div className="flex flex-col border rounded px-2 py-2">
      <span>Queued Commands</span>
      {data.map((cmd) => {
        return (
          <Card>
            <CardHeader>
              <CardTitle>{cmd.command}</CardTitle>
              <CardDescription>{cmd.message_id}</CardDescription>
              <CardContent>TBD</CardContent>
            </CardHeader>
          </Card>
        );
      })}
    </div>
  );
};

export default CommandList;
