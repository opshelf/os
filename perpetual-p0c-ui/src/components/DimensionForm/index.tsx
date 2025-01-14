import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
//import axios from 'axios';
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
import { GS1 } from '@/domain/storage';
import { updateSizes, getSize } from '@/api/sizes';
import { storeCommand } from '@/api/robot';
import { print } from '@/api/printer';

export default function DimensionForm({ data }: { data: GS1 }) {
  const [itemType, setItemState] = useState<'bottle' | 'box'>(
    'bottle'
  );
  const [itemHeight, setItemHeight] = useState('50');
  const [itemWidth, setItemWidth] = useState('50');
  const [itemLength, setItemLength] = useState('50');

  const save = async () => {
    if (itemType === 'bottle') {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemWidth),
      };
      await updateSizes(data, dimensions);
    } else {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemLength),
      };
      await updateSizes(data, dimensions);
    }
  };

  const store = async () => {
    if (itemType === 'bottle') {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemWidth),
      };
      await updateSizes(data, dimensions);
      await storeCommand(data);
    } else {
      const dimensions = {
        height: parseFloat(itemHeight),
        width: parseFloat(itemWidth),
        length: parseFloat(itemLength),
      };
      await updateSizes(data, dimensions);
      await storeCommand(data);
    }
  };

  const printLabel = async () => {
    await print(data);
  };

  const checkItemSize = async () => {
    try {
      const size = await getSize(data);
      setItemHeight(size.height.toString());
      setItemWidth(size.width.toString());
      setItemLength(size.length.toString());
    } catch (err) {
      setItemHeight('');
      setItemWidth('');
      setItemLength('');
      console.log(err);
    }
  };

  useEffect(() => {
    checkItemSize();
  }, [data]);

  return (
    <Card className="w-fit">
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
          <Button type="button" onClick={() => printLabel()}>
            Print Label
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
