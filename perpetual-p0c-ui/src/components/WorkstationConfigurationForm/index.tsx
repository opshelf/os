import { useFormContext } from 'react-hook-form';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../ui/accordion';
import AxisForm from '../AxisForm';
import {
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';

const WorkstationConfigurationEditor = ({
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
          name={`${fieldName}.input_belt_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Input Belt Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                {"The input belt speed in mm's"}
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldName}.output_belt_speed`}
          render={({ field }) => (
            <FormItem>
              <FormLabel>Output Belt Speed</FormLabel>
              <FormControl>
                <Input {...field} />
              </FormControl>
              <FormDescription>
                {"The output belt speed in mm's"}
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <Accordion type="multiple">
        <AccordionItem value={`input_belt`}>
          <AccordionTrigger>
            input belt axis configuration
          </AccordionTrigger>
          <AccordionContent>
            <AxisForm
              fieldName={`workstation.input_belt_configuration.configuration`}
            />
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value={`output_belt`}>
          <AccordionTrigger>
            output belt axis configuration
          </AccordionTrigger>
          <AccordionContent>
            <AxisForm
              fieldName={`workstation.output_belt_configuration.configuration`}
            />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
};

export default WorkstationConfigurationEditor;
