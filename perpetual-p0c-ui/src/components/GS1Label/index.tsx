import { Card, CardTitle, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { useForm } from 'react-hook-form';
import { format, addYears } from 'date-fns';
import { Calendar } from '@/components/ui/calendar';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { CalendarIcon, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import { print } from '@/api/printer';
import { toast } from '@/hooks/use-toast';

// Define types
interface GS1FormValues {
  gtin: string;
  expiry: Date;
  lot: string;
  sn: string;
}

// Function to generate random alphanumeric string
const generateRandomString = (length: number): string => {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  let result = '';
  for (let i = 0; i < length; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
};

const GS1Label = () => {
  // Set default date to one year from now
  const defaultDate = addYears(new Date(), 1);

  // Generate initial random values
  const initialLot = generateRandomString(8);
  const initialSN = generateRandomString(12);

  // Define form with default values
  const form = useForm<GS1FormValues>({
    defaultValues: {
      gtin: "00000000000000",
      expiry: defaultDate,
      lot: initialLot,
      sn: initialSN
    },
  });

  const regenerateRandomValues = (): void => {
    const newLot = generateRandomString(8);
    const newSN = generateRandomString(12);

    form.setValue('lot', newLot);
    form.setValue('sn', newSN);
  };

  const handlePrint = async (): Promise<void> => {
    const data = form.getValues();
    const formattedData = {
      ...data,
      expiry: format(data.expiry, 'yyMMdd')
    };
    const d = String.fromCharCode(29);
    try {
      await print({
        ...formattedData,
        raw_text: `01${formattedData.gtin}10${formattedData.lot}${d}21${formattedData.sn}${d}17${formattedData.expiry}`
      })
      toast({
        description: "Print Success"
      })
    } catch (err) {
      toast({
        variant: "destructive",
        description: "Error Printing Label"

      })
    }

    console.log("Printing GS1 label with data:", formattedData);
    // Printing functionality would be implemented here
  };

  // Validation rules based on GS1 standards
  const validateGTIN = (value: string): boolean | string => {
    // GTIN-14 format (14 digits)
    return /^\d{14}$/.test(value) || "GTIN must be exactly 14 digits";
  };

  const validateLot = (value: string): boolean | string => {
    // Lot number (alphanumeric, 1-20 characters)
    return /^[A-Za-z0-9]{1,20}$/.test(value) || "Lot must be 1-20 alphanumeric characters";
  };

  const validateSN = (value: string): boolean | string => {
    // Serial number (alphanumeric, 1-20 characters)
    return /^[A-Za-z0-9]{1,20}$/.test(value) || "Serial number must be 1-20 alphanumeric characters";
  };

  return (
    <Card className="w-96">
      <CardHeader>
        <CardTitle>GS1 Label Data</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <Form {...form}>
          <form className="space-y-4">
            <FormField
              control={form.control}
              name="gtin"
              rules={{
                required: "GTIN is required",
                validate: validateGTIN
              }}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>GTIN (14 digits)</FormLabel>
                  <FormControl>
                    <Input placeholder="Enter 14 digit GTIN" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="expiry"
              rules={{ required: "Expiry date is required" }}
              render={({ field }) => (
                <FormItem className="flex flex-col">
                  <FormLabel>Expiry Date</FormLabel>
                  <Popover>
                    <PopoverTrigger asChild>
                      <FormControl>
                        <Button
                          variant="outline"
                          className={cn(
                            "w-full pl-3 text-left font-normal",
                            !field.value && "text-muted-foreground"
                          )}
                        >
                          {field.value ? (
                            format(field.value, "PPP")
                          ) : (
                            <span>Pick a date</span>
                          )}
                          <CalendarIcon className="ml-auto h-4 w-4 opacity-50" />
                        </Button>
                      </FormControl>
                    </PopoverTrigger>
                    <PopoverContent className="w-auto p-0" align="start">
                      <Calendar
                        mode="single"
                        selected={field.value}
                        onSelect={field.onChange}
                        initialFocus
                      />
                    </PopoverContent>
                  </Popover>
                  <div className="text-xs text-muted-foreground">
                    Will be formatted as: {field.value ? format(field.value, 'yyMMdd') : '------'}
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="flex justify-between items-end gap-2">
              <FormField
                control={form.control}
                name="lot"
                rules={{
                  required: "Lot number is required",
                  validate: validateLot
                }}
                render={({ field }) => (
                  <FormItem className="flex-1">
                    <FormLabel>Lot Number</FormLabel>
                    <FormControl>
                      <Input placeholder="Enter lot number" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="sn"
                rules={{
                  required: "Serial number is required",
                  validate: validateSN
                }}
                render={({ field }) => (
                  <FormItem className="flex-1">
                    <FormLabel>Serial Number</FormLabel>
                    <FormControl>
                      <Input placeholder="Enter serial number" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <Button
                type="button"
                variant="outline"
                size="icon"
                onClick={regenerateRandomValues}
                className="mb-0.5"
                title="Generate random values"
              >
                <RefreshCw className="h-4 w-4" />
              </Button>
            </div>

            <Button
              type="button"
              className="w-full mt-4"
              onClick={() => handlePrint()}
            >
              Print
            </Button>
          </form>
        </Form>
      </CardContent>
    </Card>
  );
};

export default GS1Label;
