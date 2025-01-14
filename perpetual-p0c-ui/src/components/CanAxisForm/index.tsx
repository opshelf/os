import { useMemo } from 'react';
import z from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Button } from '@/components/ui/button';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';

export const stepperMotorSchema = z.object({
  microsteps: z.number(),
  steps_per_rev: z.number(),
  steps_per_mm: z.coerce.number(),
  work_current: z.number(),
  holding_current: z.number(),
  homing_current: z.number(),
  velocity: z.number(),
  acceleration: z.number(),
  encoder_ppr: z.coerce.number().nullable(),
  max_velocity: z.coerce.number().nullable(),
  max_acceleration: z.coerce.number().nullable(),
  min_travel: z.coerce.number().nullable(),
  max_travel: z.coerce.number().nullable(),
  invert_coordinates: z.boolean(),
  home_direction: z.enum(['CW', 'CCW']),
  homing_speed: z.number(),
  brake: z.boolean(),
  safety: z.boolean(),
  external_encoder: z.boolean(),
  max_encoder_deviation: z.number(),
  max_encoder_deviation_homing: z.number(),
});

export type StepperMotorSchema = z.infer<typeof stepperMotorSchema>;

export const motorStatusSchema = z.object({
  hard_stopped: z.boolean(),
  safety_on: z.boolean(),
  homed: z.boolean(),
  error: z.boolean(),
  position: z.number(),
});

export type MotorStatusSchema = z.infer<typeof motorStatusSchema>;

export const axisSchema = z.object({
  can_id: z.number(),
  name: z.string(),
  axis_type: z.string(),
  configuration: stepperMotorSchema,
});

export type AxisSchema = z.infer<typeof axisSchema>;

const StepperMotorForm = ({
  config,
}: {
  config: StepperMotorSchema;
}) => {
  const form = useForm<StepperMotorSchema>({
    resolver: zodResolver(stepperMotorSchema),
    defaultValues: useMemo(() => {
      return config;
    }, [config]),
  });

  const handleSubmit = (value: StepperMotorSchema) => {
    console.log(value);
  };

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(handleSubmit)}
        className="space-y-4"
      >
        <div className="flex items-center space-x-4">
          <FormField
            control={form.control}
            name="microsteps"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Microsteps</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('microsteps', {
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
            name="steps_per_rev"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Steps Per Rev</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('steps_per_rev', {
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
            name="encoder_ppr"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Encoder PPR</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    value={field.value === null ? '' : field.value}
                    onChange={(e) => {
                      const value = e.target.value;
                      field.onChange(
                        value === '' ? null : Number(value)
                      );
                    }}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="steps_per_mm"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Steps per mm</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('steps_per_mm', {
                      valueAsNumber: true,
                    })}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
        </div>
        <div className="flex items-center space-x-4">
          <FormField
            control={form.control}
            name="work_current"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Work Current</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('work_current', {
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
            name="holding_current"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Holding Current</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('holding_current', {
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
            name="homing_current"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Homing Current</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('homing_current', {
                      valueAsNumber: true,
                    })}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
        </div>
        <div className="flex items-center space-x-4">
          <FormField
            control={form.control}
            name="velocity"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Velocity</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('velocity', {
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
            name="acceleration"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Acceleration</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    {...field}
                    {...form.register('acceleration', {
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
            name="max_velocity"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Max Velocity</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    value={field.value === null ? '' : field.value}
                    onChange={(e) => {
                      const value = e.target.value;
                      field.onChange(
                        value === '' ? null : Number(value)
                      );
                    }}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="max_acceleration"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Max Acceleration</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    value={field.value === null ? '' : field.value}
                    onChange={(e) => {
                      const value = e.target.value;
                      field.onChange(
                        value === '' ? null : Number(value)
                      );
                    }}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
        </div>
        <div className="flex items-center space-x-4">
          <FormField
            control={form.control}
            name="max_travel"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Max Travel</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    value={field.value === null ? '' : field.value}
                    onChange={(e) => {
                      const value = e.target.value;
                      field.onChange(
                        value === '' ? null : Number(value)
                      );
                    }}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="min_travel"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Minimum Travel</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    value={field.value === null ? '' : field.value}
                    onChange={(e) => {
                      const value = e.target.value;
                      field.onChange(
                        value === '' ? null : Number(value)
                      );
                    }}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="homing_speed"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Homing Speed</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    placeholder=""
                    className="w-fit"
                    {...field}
                    value={`${field.value}`}
                    {...form.register(`homing_speed`, {
                      valueAsNumber: true,
                      setValueAs: (v) =>
                        v === '' ? null : parseInt(v),
                    })}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
        </div>
        <div className="flex items-start space-x-4">
          <FormField
            control={form.control}
            name="invert_coordinates"
            render={({ field }) => (
              <FormItem className="flex flex-col">
                <FormLabel>Invert Coordinates</FormLabel>
                <FormControl>
                  <Switch
                    checked={field.value}
                    onCheckedChange={field.onChange}
                  />
                </FormControl>
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="home_direction"
            render={({ field }) => (
              <FormItem className="flex flex-col relative">
                <FormLabel>Home Direction</FormLabel>
                <div className="flex items-center space-x-4 w-full ">
                  <FormControl>
                    <Switch
                      checked={field.value == 'CCW'}
                      onCheckedChange={(_) => {
                        if (field.value == 'CCW') {
                          field.onChange('CW');
                        } else {
                          field.onChange('CCW');
                        }
                      }}
                    />
                  </FormControl>
                  <p className="text-lg">{field.value}</p>
                </div>
              </FormItem>
            )}
          />
        </div>
        <Button type="submit">Update Configuration</Button>
      </form>
    </Form>
  );
};

export default StepperMotorForm;
