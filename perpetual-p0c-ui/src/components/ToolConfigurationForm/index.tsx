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

const ToolConfigurationEditor = ({
  fieldName,
}: {
  fieldName: string;
}) => {
  const form = useFormContext();

  return (
    <div className="flex flex-col space-y-2">
      <FormField
        control={form.control}
        name={`${fieldName}.tool_retract_position`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tool Retract Position</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>
              {'Tool Retract Position'}
            </FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tool_extend_position`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tool Extend Position</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>
              {'Tool Extend Position'}
            </FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tool_safe_min`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tool Safe Min</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>{'Tool Safe Min'}</FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tool_safe_max`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tool Safe Max</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>{'Tool Safe Max'}</FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tray_pull_offset`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tray Pull Offset</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>
              {'The Position the Tray is Pulled to'}
            </FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tray_pull_right`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tray Pull Offset</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>
              {'Tray Pull position when pulling from Right Module'}
            </FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={form.control}
        name={`${fieldName}.tray_pull_left`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Tray Pull Offset</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>
              {'Tray Pull position when pulling from Left Module'}
            </FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <Accordion type="multiple">
        <AccordionItem value={`door`}>
          <AccordionTrigger>
            carriage axis configuration
          </AccordionTrigger>
          <AccordionContent>
            <AxisForm
              fieldName={`tool_configuration.carriage.configuration`}
            />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
};
export default ToolConfigurationEditor;
