import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../ui/accordion';
import AxisForm from '../AxisForm';

const GantryConfigurationEditor = ({
  fieldName,
}: {
  fieldName: string;
}) => {
  return (
    <Accordion type="multiple">
      <AccordionItem value={`x_axis`}>
        <AccordionTrigger>x axis configuration</AccordionTrigger>
        <AccordionContent>
          <AxisForm fieldName={`${fieldName}.x_axis.configuration`} />
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value={`y_axis`}>
        <AccordionTrigger>y axis configuration</AccordionTrigger>
        <AccordionContent>
          <AxisForm fieldName={`${fieldName}.y_axis.configuration`} />
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
};

export default GantryConfigurationEditor;
