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

const OutputBoxConfigurationEditor = ({
  fieldName,
}: {
  fieldName: string;
}) => {
  const form = useFormContext();

  return (
    <div className="flex flex-col space-y-2">
      <FormField
        control={form.control}
        name={`${fieldName}.door_open_position`}
        render={({ field }) => (
          <FormItem>
            <FormLabel>Door Open Position</FormLabel>
            <FormControl>
              <Input {...field} />
            </FormControl>
            <FormDescription>{'Door Open Position'}</FormDescription>
            <FormMessage />
          </FormItem>
        )}
      />
      <Accordion type="multiple">
        <AccordionItem value={`door`}>
          <AccordionTrigger>door axis configuration</AccordionTrigger>
          <AccordionContent>
            <AxisForm
              fieldName={`workstation.outputbox_configuration.door.configuration`}
            />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
};
export default OutputBoxConfigurationEditor;
