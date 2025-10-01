import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { robotConfigurationSchema } from "@/domain/configuration";
import type { RobotConfiguration } from "@/domain/configuration";
import { getConfiguration, setConfiguration } from "@/api/robot";

export function RobotConfigurationForm({
  initialValues,
  onSubmit,
}: {
  initialValues: RobotConfiguration;
  onSubmit: (data: RobotConfiguration) => void;
}) {
  const form = useForm<RobotConfiguration>({
    resolver: zodResolver(robotConfigurationSchema),
    defaultValues: initialValues,
    mode: "onChange",
  });

  const { control, handleSubmit } = form;

  return (
    <Form {...form}>
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-8">
        <Card>
          <CardHeader>
            <CardTitle>Robot Configuration</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            <FormField
              control={control}
              name="y_safe"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Y Safe Height</FormLabel>
                  <FormControl>
                    <Input type="number" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={control}
              name="state_file"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>State File Path</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            {[
              "idle_position",
              "place_offset",
              "pickup_location",
              "pickup_location_offset",
            ].map((coordKey) => (
              <div key={coordKey}>
                <FormLabel className="text-lg capitalize">
                  {coordKey.replace(/_/g, " ")}
                </FormLabel>
                <div className="grid grid-cols-3 gap-2">
                  {(["x", "y"] as const).map((axis) => (
                    <FormField
                      key={`${coordKey}.${axis}`}
                      control={control}
                      name={`${coordKey}.${axis}` as any}
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel className="uppercase">{axis}</FormLabel>
                          <FormControl>
                            <Input type="number" {...field} />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  ))}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Tool Configuration</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            {(["min_width", "max_width", "gripper_pickup_width"] as const).map(
              (gripField) => (
                <FormField
                  key={`tool.gripper.${gripField}`}
                  control={control}
                  name={`tool.gripper.${gripField}` as any}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="capitalize">
                        {gripField.replace(/_/g, " ")}
                      </FormLabel>
                      <FormControl>
                        <Input type="number" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              ),
            )}

            {(
              [
                "tool_shelf_offset",
                "tool_retracted_offset",
                "tool_extension_offset",
                "tool_pickup_extension",
              ] as const
            ).map((toolField) => (
              <FormField
                key={`tool.${toolField}`}
                control={control}
                name={`tool.${toolField}` as any}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="capitalize">
                      {toolField.replace(/_/g, " ")}
                    </FormLabel>
                    <FormControl>
                      <Input type="number" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            ))}
          </CardContent>
        </Card>

        <div className="flex justify-end">
          <Button type="submit" className="w-40">
            Save Configuration
          </Button>
        </div>
      </form>
    </Form>
  );
}

const RobotConfiguration = () => {
  const queryClient = useQueryClient();
  const {
    data,
    isPending,
    isError,
    error,
  } = useQuery({
    queryKey: ["robot-configuration"],
    queryFn: getConfiguration,
  });

  const mutation = useMutation({
    mutationFn: setConfiguration,
    onSuccess: (updated) => {
      queryClient.setQueryData(["robot-configuration"], updated);
    },
  });

  if (isPending) {
    return <p className="p-4">Loading robot configuration…</p>;
  }

  if (isError || !data) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return (
      <p className="p-4 text-destructive">
        Failed to load configuration: {message}
      </p>
    );
  }

  return (
    <RobotConfigurationForm
      initialValues={data}
      onSubmit={async (values) => {
        await mutation.mutateAsync(values);
      }}
    />
  );
};

export default RobotConfiguration;
