import z from 'zod';
import { Button } from '@/components/ui/button';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm, useFormContext } from 'react-hook-form';
import { useMemo } from 'react';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../ui/accordion';
import { getConfiguration, setConfiguration } from '@/api/robot';
import { useQuery } from '@tanstack/react-query';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import FunnelConfigurationEditor from '../FunnelConfigurationForm';
import WorkstationConfigurationEditor from '../WorkstationConfigurationForm';
import OutputBoxConfigurationEditor from '../OutputBoxConfigurationForm';
import GantryConfigurationEditor from '../GantryConfigurationForm';
import ToolConfigurationEditor from '../ToolConfigurationForm';
import { robotConfigurationSchema } from '@/domain/configuration';

const GantryCoordinateEditor = ({
  fieldId,
  fieldName,
  description,
}: {
  fieldId: string;
  fieldName: string;
  description: string;
}) => {
  const form = useFormContext();
  return (
    <div className="flex flex-col space-y-2">
      <FormLabel>{fieldName}</FormLabel>
      <div className="flex items-start space-x-2">
        <FormField
          control={form.control}
          name={`${fieldId}.x`}
          render={({ field }) => (
            <FormItem>
              <div className="flex items-center gap-2">
                <FormLabel>X:</FormLabel>
                <FormControl>
                  <Input {...field} />
                </FormControl>
              </div>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name={`${fieldId}.y`}
          render={({ field }) => (
            <FormItem>
              <div className="flex items-center gap-2">
                <FormLabel>Y:</FormLabel>
                <FormControl>
                  <Input {...field} />
                </FormControl>
              </div>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
      <FormDescription>{description}</FormDescription>
    </div>
  );
};

const ConfigurationEditor = ({
  configuration,
}: {
  configuration: z.infer<typeof robotConfigurationSchema>;
}) => {
  const form = useForm<z.infer<typeof robotConfigurationSchema>>({
    resolver: zodResolver(robotConfigurationSchema),
    defaultValues: useMemo(() => {
      return configuration;
    }, [configuration]),
  });

  async function onSubmit(
    values: z.infer<typeof robotConfigurationSchema>
  ) {
    await setConfiguration(values);
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)}>
        <Accordion type="multiple">
          <AccordionItem value="robot">
            <AccordionTrigger>Robot</AccordionTrigger>
            <AccordionContent>
              <div className="flex flex-col space-y-4">
                <div className="flex items-end space-x-4">
                  <FormField
                    control={form.control}
                    name="gantry_safe_x"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Gantry Safe X</FormLabel>
                        <FormControl>
                          <Input {...field} />
                        </FormControl>
                        <FormDescription>
                          Minimum X Position where all Y travel is
                          valid
                        </FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="gantry_safe_y"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Gantry Safe Y</FormLabel>
                        <FormControl>
                          <Input {...field} />
                        </FormControl>
                        <FormDescription>
                          The y position in which all x travel with a
                          tray is valid
                        </FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="gantry_safe_y_high_clearance"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Gantry Safe Y High Clearance
                        </FormLabel>
                        <FormControl>
                          <Input {...field} />
                        </FormControl>
                        <FormDescription>
                          The y position in which all x travel above
                          the funnel is valid
                        </FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </div>
                <div className="flex items-end space-x-4">
                  <GantryCoordinateEditor
                    fieldId="place_offset"
                    fieldName="Place Offset"
                    description="Placement Offset for Returning trays"
                  />
                  <GantryCoordinateEditor
                    fieldId="service_coordinate_high"
                    fieldName="Service High Coordinate"
                    description="Position to Send Gantry to for servicing the tool base"
                  />
                </div>
                <div className="flex items-end space-x-4">
                  <GantryCoordinateEditor
                    fieldId="idle_coordinate"
                    fieldName="Idle Coordinate"
                    description="Position to Send Gantry to using Idle Command"
                  />
                  <GantryCoordinateEditor
                    fieldId="output_location"
                    fieldName="Output Location"
                    description="Position used to drop items off on the gantry"
                  />
                </div>
                <GantryCoordinateEditor
                  fieldId="input_location"
                  fieldName="Input Location"
                  description="Position used to receive items final position is calculated by subtracting the tray height from this coordinate"
                />
              </div>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="funnel">
            <AccordionTrigger>Funnel</AccordionTrigger>
            <AccordionContent>
              <FunnelConfigurationEditor fieldName="funnel_configuration" />
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="workstation">
            <AccordionTrigger>Workstation</AccordionTrigger>
            <AccordionContent>
              <WorkstationConfigurationEditor fieldName="workstation" />
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="tool">
            <AccordionTrigger>Tool</AccordionTrigger>
            <AccordionContent>
              <ToolConfigurationEditor fieldName="tool_configuration" />
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="gantry">
            <AccordionTrigger>Gantry</AccordionTrigger>
            <AccordionContent>
              <GantryConfigurationEditor fieldName="gantry_configuration" />
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="outputbox">
            <AccordionTrigger>Output Box</AccordionTrigger>
            <AccordionContent>
              <OutputBoxConfigurationEditor fieldName="workstation.outputbox_1_configuration" />
              <OutputBoxConfigurationEditor fieldName="workstation.outputbox_2_configuration" />
              <OutputBoxConfigurationEditor fieldName="workstation.outputbox_3_configuration" />
            </AccordionContent>
          </AccordionItem>
        </Accordion>
        <Button type="submit" className="mt-2">
          Save
        </Button>
      </form>
    </Form>
  );
};

const RobotConfiguration = () => {
  const { isPending, error, data } = useQuery({
    queryKey: ['configuration'],
    queryFn: getConfiguration,
  });
  if (isPending) return 'Loading...';

  if (error) return 'An error has occurred: ' + error.message;
  console.log(data);
  const robotData = robotConfigurationSchema.parse(data);

  return <ConfigurationEditor configuration={robotData} />;
};
export default RobotConfiguration;
