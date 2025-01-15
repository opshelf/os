import { Switch } from '../ui/switch';
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { useFormContext } from 'react-hook-form';

const AxisForm = ({ fieldName }: { fieldName: string }) => {
  const form = useFormContext();
  return (
    <div className="flex flex-col space-y-2">
      <div className="flex items-center space-x-4">
        <FormField
          control={form.control}
          name={`${fieldName}.microsteps`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Microsteps</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.microsteps`, {
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
          name={`${fieldName}.steps_per_rev`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Steps Per Rev</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}steps_per_rev`, {
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
          name={`${fieldName}.encoder_ppr`}
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
          name={`${fieldName}.steps_per_mm`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Steps per mm</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.steps_per_mm`, {
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
          name={`${fieldName}.work_current`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Work Current</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.work_current`, {
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
          name={`${fieldName}.holding_current`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Holding Current</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}holding_current`, {
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
          name={`${fieldName}.homing_current`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Homing Current</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.homing_current`, {
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
          name={`${fieldName}.velocity`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Velocity</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.velocity`, {
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
          name={`${fieldName}.acceleration`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Acceleration</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  {...field}
                  {...form.register(`${fieldName}.acceleration`, {
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
          name={`${fieldName}.max_velocity`}
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
          name={`${fieldName}.max_acceleration`}
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
          name={`${fieldName}.max_travel`}
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
          name={`${fieldName}.min_travel`}
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
          name={`${fieldName}.homing_speed`}
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
                  {...form.register(`${fieldName}.homing_speed`, {
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
          name={`${fieldName}.invert_coordinates`}
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
          name={`${fieldName}.home_direction`}
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
        <FormField
          control={form.control}
          name={`${fieldName}.safety`}
          render={({ field }) => (
            <FormItem className="flex flex-col relative">
              <FormLabel>Safety</FormLabel>
              <div className="flex items-center space-x-4 w-full ">
                <FormControl>
                  <Switch
                    checked={field.value}
                    onCheckedChange={field.onChange}
                  />
                </FormControl>
                <p className="text-lg">{field.value}</p>
              </div>
            </FormItem>
          )}
        />
      </div>
    </div>
  );
};

export default AxisForm;
