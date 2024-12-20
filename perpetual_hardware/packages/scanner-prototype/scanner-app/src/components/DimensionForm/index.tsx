import { useState } from 'react';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import axios from 'axios';
//import {
//  Form,
//  FormControl,
//  FormDescription,
//  FormField,
//  FormItem,
//  FormLabel,
//  FormMessage,
//} from '@/components/ui/form';
import {
  Select,
  SelectItem,
  SelectContent,
  SelectValue,
  SelectTrigger,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '../ui/card';
import { GS1Data } from '@/store';

const formSchema = z.object({
  height: z.number().min(0),
  width: z.number().min(0),
  length: z.number().min(0),
});

export default function DimensionForm({ data }: { data: GS1Data }) {
  //const form = useForm<z.infer<typeof formSchema>>({
  //  resolver: zodResolver(formSchema),
  //  defaultValues: {
  //    username: '',
  //  },
  //});

  //// 2. Define a submit handler.
  //function onSubmit(values: z.infer<typeof formSchema>) {
  //  // Do something with the form values.
  //  // ✅ This will be type-safe and validated.
  //  console.log(values);
  //}
  const [itemType, setItemState] = useState<'bottle' | 'box'>(
    'bottle'
  );
  const [itemHeight, setItemHeight] = useState('50');
  const [itemWidth, setItemWidth] = useState('50');
  const [itemLength, setItemLength] = useState('50');
  /*

    @app.post("/api/store")
    async def save_dimensions_and_run(
            data: GS1Data, 
            dimensions: Dimensions, 
            worker=Depends(get_worker), 
            incoming_queue=Depends(get_incoming_message_queue)
    ) -> dict[str, str]:

    @app.post("/api/update_dimensions")
    async def update_dimensions(
            data: GS1Data, 
            dimensions: Dimensions, 
            worker=Depends(get_worker), 
            #incoming_queue=Depends(get_incoming_message_queue)
    ) -> dict[str, str]:
      
  */

  const save = async () => {
    if (itemType === 'bottle') {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemWidth),
      };
      await axios.post(
        'http://localhost:8000/api/update_dimensions',
        {
          data,
          dimensions,
        }
      );
    } else {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemLength),
      };
      await axios.post(
        'http://localhost:8000/api/update_dimensions',
        {
          data,
          dimensions,
        }
      );
    }
  };

  const store = async () => {
    if (itemType === 'bottle') {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemWidth),
      };
      await axios.post('http://localhost:8000/api/store', {
        data,
        dimensions,
      });
    } else {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemLength),
      };
      await axios.post('http://localhost:8000/api/store', {
        data,
        dimensions,
      });
    }
  };

  return (
    <Card className="w-full">
      <CardHeader>
        <CardTitle>Object Dimensions</CardTitle>
        <CardDescription>
          <p>
            Enter the Scanned Objects Size or Edit it if Necessary
          </p>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col space-y-4">
        <Label>Item Type:</Label>
        <Select
          onValueChange={(value) =>
            setItemState(value as 'bottle' | 'box')
          }
          value={itemType}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="bottle">Bottle</SelectItem>
            <SelectItem value="box">Box</SelectItem>
          </SelectContent>
        </Select>
        <Label>Height:</Label>
        <Input
          className="w-[180px]"
          value={itemHeight}
          onChange={(e) => setItemHeight(e.target.value)}
        />
        <Label>Width:</Label>
        <Input
          className="w-[180px]"
          value={itemWidth}
          onChange={(e) => setItemWidth(e.target.value)}
        />
        {itemType === 'box' && (
          <>
            <Label>Length:</Label>
            <Input
              className="w-[180px]"
              value={itemLength}
              onChange={(e) => setItemLength(e.target.value)}
            />
          </>
        )}

        <div className="flex space-x-4">
          <Button type="button" onClick={() => save()}>
            Save
          </Button>
          <Button type="button" onClick={() => store()}>
            Save And Store
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/*
<Form {...form}>
  <form
    onSubmit={form.handleSubmit(onSubmit)}
    className="space-y-8"
  >
    <FormField
      control={form.control}
      name="username"
      render={({ field }) => (
        <FormItem>
          <FormLabel>Username</FormLabel>
          <FormControl>
            <Input placeholder="shadcn" {...field} />
          </FormControl>
          <FormDescription>
            This is your public display name.
          </FormDescription>
          <FormMessage />
        </FormItem>
      )}
    />
    <Button type="submit">Submit</Button>
  </form>
</Form>
*/
