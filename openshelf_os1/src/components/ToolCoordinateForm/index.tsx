import { Button } from '@/components/ui/button';
import { useForm } from 'react-hook-form';
import { useMemo } from 'react';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import z from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { useStateStore } from '@/store';

const toolCoordinateSchema = z.object({
  x: z.number(),
  y: z.number(),
  z: z.number(),
});

export type ToolCoordinateSchema = z.infer<
  typeof toolCoordinateSchema
>;

export const ToolCoordinateForm = ({
  success,
  move,
  point,
}: {
  success: (toolCoordiante: ToolCoordinateSchema) => void;
  move: (toolCoordiante: ToolCoordinateSchema) => Promise<void>;
  point: ToolCoordinateSchema | null;
}) => {
  const { robotStatus } = useStateStore();

  const gantryStatus = robotStatus?.robot_status?.gantry_status;

  const form = useForm<ToolCoordinateSchema>({
    resolver: zodResolver(toolCoordinateSchema),
    defaultValues: useMemo(() => {
      if (point !== null) {
        return point;
      } else {
        return {
          x: 0,
          y: 0,
          z: 0,
        };
      }
    }, [point]),
  });

  async function onSubmit(values: ToolCoordinateSchema) {
    success(values);
  }

  async function onMove() {
    form.handleSubmit(async (data) => {
      await move(data);
    })();
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className="flex flex-col gap-y-2 items-start"
      >
        <FormField
          control={form.control}
          name="x"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormLabel>X: </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`x`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="y"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormLabel>Y: </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`y`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="z"
          render={({ field }) => (
            <FormItem className="flex items-center space-x-2">
              <FormLabel>Z: </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  className="w-24"
                  {...form.register(`z`, {
                    valueAsNumber: true,
                  })}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <div className="flex gap-2">
          {gantryStatus !== undefined && (
            <>
              <Button
                type="button"
                className="w-full"
                onClick={onMove}
              >
                Move
              </Button>
              <Button
                type="button"
                className="w-full"
                onClick={() => {
                  form.setValue('x', gantryStatus.x_axis.position);
                  form.setValue('y', gantryStatus.y_axis.position);
                }}
              >
                Load
              </Button>
            </>
          )}
          <Button type="submit" className="w-full">
            Save
          </Button>
        </div>
      </form>
    </Form>
  );
};
