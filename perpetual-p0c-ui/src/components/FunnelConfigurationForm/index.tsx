import { useFormContext } from 'react-hook-form';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../ui/accordion';
import {
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import AxisForm from '../AxisForm';

const FunnelConfigurationEditor = ({
  fieldName,
}: {
  fieldName: string;
}) => {
  const form = useFormContext();
  return (
    <div className="flex flex-col space-y-2">
      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldName}.funnel_min_width`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Funnel Min Width</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The minimum attainable funnel width
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.funnel_cell_1_offset_x`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Funnel Cell 1 Offset</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The offset of the inlet funnel to cell 1
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.catch_position`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Catch Position</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The catch position of the opposing funnel when an item
                is sent to be retrieved
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>

      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldName}.diverter_50mm_pos`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Diverter 50mm Position</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The 50mm Opening Position of the diverter
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.diverter_100mm_pos`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Diverter 100mm Position</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The 100mm Opening Position of the diverter
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.diverter_150mm_pos`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Diverter 150mm Position</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The 150mm Opening Position of the diverter
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldName}.belt_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Input Belt Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The Speed in mm's of the input belt
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.wall_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Wall Belt Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The Speed in mm's of the wall belt
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.output_transfer_conveyor_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Output Transfer Conveyor Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The Speed in mm's of the output transfer conveyor belt
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldName}.output_conveyor_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Output Conveyor Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The Speed in mm's of the output conveyor belt
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.slot_gap_size`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Slot Gap Size</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The gap size between the input conveyor and the
                extended slot
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.opposing_funnel_max_travel`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Opposing Funnel Max Travel</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The distance from the home point to the edge of cell 1
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldName}.input_funnel_max_travel`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Input Funnel Max Travel</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The distance from the home point to the point of
                collision with the opposing funnel in the home
                position
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.minimum_transfer_location`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Minimum Transfer Location</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                The minimum point at which an item can be transferred
                to the output conveyor
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <Accordion type="multiple">
        {[
          'inlet_funnel',
          'opposing_funnel',
          'slot_1',
          'slot_2',
          'slot_3',
          'belt',
          'wall',
          'output_transfer_conveyor',
          'output_conveyor',
          'diverter',
        ].map((axisName) => {
          return (
            <AccordionItem value={`${axisName}`} key={axisName}>
              <AccordionTrigger>
                {axisName.split('_').join(' ')} axis configuration
              </AccordionTrigger>
              <AccordionContent>
                <AxisForm
                  fieldName={`funnel_configuration.${axisName}.configuration`}
                />
              </AccordionContent>
            </AccordionItem>
          );
        })}
      </Accordion>
    </div>
  );
};

export default FunnelConfigurationEditor;
