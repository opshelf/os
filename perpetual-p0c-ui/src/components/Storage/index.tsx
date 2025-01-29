import { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  CrosshairIcon,
  DeleteIcon,
  Edit,
  MinusIcon,
  PlusIcon,
  Settings,
} from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import {
  ToolCoordinateForm,
  ToolCoordinateSchema,
} from '@/components/ToolCoordinateForm';
import {
  Tray,
  ShelfData,
  StorageModuleData,
  StorageModuleSide,
} from '@/domain';
import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query';
import { getStorage, updateStorage } from '@/api';
import { Switch } from '../ui/switch';
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerFooter,
  DrawerTrigger,
  DrawerHeader,
} from '@/components/ui/drawer';
import GantryController from '../GantryController';
import ToolController from '../ToolController';

const Shelf = ({
  storageModule,
  shelfIdx,
  side,
  updateStorageModule,
}: {
  storageModule: StorageModuleData;
  shelfIdx: number;
  side: StorageModuleSide;
  updateStorageModule: (storageModule: StorageModuleData) => void;
}) => {
  const defaultTray = (): Tray => {
    return {
      width: 50,
      height: storageModule[side][shelfIdx].shelf_height,
      calibration_point: null,
      empty: true,
      slots: [],
    };
  };

  const deleteTray = (idx: number) => {
    let updated = { ...storageModule };
    updated[side][shelfIdx].trays.splice(idx, 1);
    updateStorageModule(updated);
  };

  const addTray = (idx: number) => {
    let updated = { ...storageModule };
    updated[side][shelfIdx].trays.splice(idx + 1, 0, defaultTray());
    updateStorageModule(updated);
  };

  const moveToToolPoint = async (point: ToolCoordinateSchema) => {
    console.log(point);
  };

  const shelfHeight = storageModule[side][shelfIdx].shelf_height;
  const shelfWidth = storageModule[side][shelfIdx].shelf_width;
  return (
    <div
      className="flex justify-center w-full items-center gap-2 border-b"
      style={{
        height: `${Math.round(
          storageModule[side][shelfIdx].shelf_height
        )}px`,
      }}
    >
      <div className="flex grow flex-row-reverse h-full items-end px-2 py-2 gap-2">
        {!storageModule[side][shelfIdx].trays.length && (
          <div className="flex border rounded items-center justify-center relative h-full w-1/8">
            <Button
              variant={'ghost'}
              className="self-center hover:bg-white"
              onClick={() => {
                let updated = { ...storageModule };
                updated[side][shelfIdx].trays = [defaultTray()];
                updateStorageModule(updated);
              }}
            >
              <PlusIcon />
            </Button>
          </div>
        )}
        {storageModule[side][shelfIdx].trays.map((tray, idx) => {
          return (
            <Popover>
              <div
                className={`flex border ${
                  !tray.empty && 'bg-slate-100'
                } rounded items-center justify-center relative`}
                style={{
                  height: `${Math.round(
                    (tray.height / shelfHeight) * 100
                  )}%`,
                  width: `${Math.round(
                    (tray.width / shelfWidth) * 100
                  )}%`,
                }}
              >
                {tray.calibration_point ? <CrosshairIcon /> : <></>}
                <div className="absolute bottom-0 left-0 w-full flex items-center justify-between px-2">
                  <Button
                    variant={'ghost'}
                    className="px-0 mx-0 hover:bg-white"
                    onClick={() => addTray(idx)}
                  >
                    <PlusIcon />
                  </Button>
                  <PopoverTrigger asChild>
                    <Button
                      variant={'ghost'}
                      className="px-0 mx-0 hover:bg-white"
                    >
                      <Edit />
                    </Button>
                  </PopoverTrigger>
                  <Button
                    variant={'ghost'}
                    className="px-0 mx-0 hover:bg-white"
                    onClick={() => deleteTray(idx)}
                  >
                    <DeleteIcon />
                  </Button>
                </div>
              </div>
              <PopoverContent>
                <div className="flex flex-col w-full gap-y-2"></div>
                <div className="flex gap-4 items-center">
                  <Label>Empty</Label>
                  <Switch
                    value={`${tray.width}`}
                    checked={tray.empty}
                    onCheckedChange={(value) => {
                      let updated = { ...storageModule };
                      updated[side][shelfIdx].trays[idx].empty =
                        value;
                      updateStorageModule(updated);
                    }}
                  />
                </div>
                <Label>Tray Width</Label>
                <Select
                  value={`${tray.width}`}
                  onValueChange={(value) => {
                    let updated = { ...storageModule };
                    updated[side][shelfIdx].trays[idx].width =
                      parseInt(value);
                    updateStorageModule(updated);
                  }}
                >
                  <SelectTrigger className="w-[180px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="50">50(mm)</SelectItem>
                    <SelectItem value="100">100(mm)</SelectItem>
                    <SelectItem value="150">150(mm)</SelectItem>
                  </SelectContent>
                </Select>
                <Label>Tray Height</Label>
                <Select
                  value={`${tray.height}`}
                  onValueChange={(value) => {
                    let updated = { ...storageModule };
                    updated[side][shelfIdx].trays[idx].height =
                      parseInt(value);
                    updateStorageModule(updated);
                  }}
                >
                  <SelectTrigger className="w-[180px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {shelfHeight >= 50 ? (
                      <SelectItem value="50">50(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 75 ? (
                      <SelectItem value="75">75(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 100 ? (
                      <SelectItem value="100">100(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 125 ? (
                      <SelectItem value="125">125(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 150 ? (
                      <SelectItem value="150">150(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 175 ? (
                      <SelectItem value="175">175(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                    {shelfHeight >= 200 ? (
                      <SelectItem value="200">200(mm)</SelectItem>
                    ) : (
                      <></>
                    )}
                  </SelectContent>
                </Select>
                <Label>Cal Point:</Label>
                <ToolCoordinateForm
                  success={(res: ToolCoordinateSchema) => {
                    let updated = { ...storageModule };
                    updated[side][shelfIdx].trays[
                      idx
                    ].calibration_point = res;
                    updateStorageModule(updated);
                  }}
                  move={moveToToolPoint}
                  point={tray.calibration_point}
                />
              </PopoverContent>
            </Popover>
          );
        })}
      </div>
    </div>
  );
};

const StorageDesigner = ({
  storageData,
}: {
  storageData: StorageModuleData;
}) => {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: updateStorage,
    onSuccess: () => {
      // Invalidate and refetch
      queryClient.invalidateQueries({ queryKey: ['storage'] });
    },
  });

  const [moduleSide, setModuleSide] =
    useState<StorageModuleSide>('right_module');

  const [storageModule, setStorageModule] =
    useState<StorageModuleData>(storageData);

  const defaultShelf = (): ShelfData => {
    return {
      shelf_height: 75,
      shelf_width: 800,
      trays: [],
    };
  };

  const deleteShelf = (idx: number) => {
    let newStorageModule = { ...storageModule };
    newStorageModule[moduleSide].splice(idx, 1);

    setStorageModule(newStorageModule);
  };

  const addShelf = (idx: number) => {
    let newStorageModule = { ...storageModule };
    newStorageModule[moduleSide].splice(idx + 1, 0, defaultShelf());
    setStorageModule(newStorageModule);
  };

  return (
    <div className="flex flex-col w-full h-full items-center px-4 justify-end h-full space-y-2">
      <span className="text-xl w-full font-semibold">
        {moduleSide === 'left_module'
          ? 'Left Module'
          : 'Right Module'}
      </span>
      <div className="flex flex-col-reverse w-full">
        {!storageModule[moduleSide].length && (
          <Button
            variant={'outline'}
            className="self-center hover:bg-white"
            onClick={() => {
              let newModule = { ...storageModule };
              newModule[moduleSide] = [defaultShelf()];
              setStorageModule(newModule);
            }}
          >
            <PlusIcon />
          </Button>
        )}
        {storageModule[moduleSide].map((shelf, idx) => {
          return (
            <div className="flex w-full items-end justify-end gap-2">
              <Shelf
                storageModule={storageModule}
                shelfIdx={idx}
                side={moduleSide}
                updateStorageModule={setStorageModule}
              />
              <Popover>
                <div className="flex items-center space-x-2 pb-2">
                  <Button
                    variant={'outline'}
                    onClick={() => addShelf(idx)}
                  >
                    <PlusIcon />
                  </Button>
                  <PopoverTrigger asChild>
                    <Button variant="secondary">
                      <Settings />
                    </Button>
                  </PopoverTrigger>
                  <Button
                    variant="destructive"
                    onClick={() => deleteShelf(idx)}
                  >
                    <MinusIcon />
                  </Button>
                </div>
                <PopoverContent>
                  <div className="flex flex-col w-full gap-y-2">
                    <Label>Shelf Height</Label>
                    <Select
                      value={`${shelf.shelf_height}`}
                      onValueChange={(value) => {
                        let newStorageModule = { ...storageModule };
                        let newHeight = parseInt(value);
                        newStorageModule[moduleSide][
                          idx
                        ].shelf_height = newHeight;
                        newStorageModule[moduleSide][idx].trays =
                          newStorageModule[moduleSide][idx].trays.map(
                            (tray) => {
                              return {
                                ...tray,
                                height: Math.min(
                                  tray.height,
                                  newHeight
                                ),
                              };
                            }
                          );
                        setStorageModule(newStorageModule);
                      }}
                    >
                      <SelectTrigger className="w-[180px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="50">50(mm)</SelectItem>
                        <SelectItem value="75">75(mm)</SelectItem>
                        <SelectItem value="100">100(mm)</SelectItem>
                        <SelectItem value="125">125(mm)</SelectItem>
                        <SelectItem value="150">150(mm)</SelectItem>
                        <SelectItem value="175">175(mm)</SelectItem>
                        <SelectItem value="200">200(mm)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </PopoverContent>
              </Popover>
            </div>
          );
        })}
      </div>
      <div className="flex items-center justify-end w-full space-x-2">
        <Button
          onClick={() => {
            setModuleSide('left_module');
          }}
        >
          Show Left Module
        </Button>
        <Button
          onClick={() => {
            setModuleSide('right_module');
          }}
        >
          Show Right Module
        </Button>
        <Button
          onClick={async () => {
            mutation.mutate(storageModule);
          }}
        >
          Save
        </Button>
      </div>
    </div>
  );
};

const Storage = () => {
  const { isPending, isError, data, error } = useQuery({
    queryKey: ['storage'],
    queryFn: getStorage,
  });

  if (isPending) {
    return <span>Loading...</span>;
  }

  if (isError) {
    return <span>Error: {error.message}</span>;
  }

  console.log(data);

  return (
    <Drawer>
      <StorageDesigner storageData={data} />
      <DrawerTrigger asChild>
        <div className="block bg-white fixed w-12 h-12 z-20 bottom-10 left-2/4 right-2/4 drop-shadow-2xl">
          <div className="relative h-full w-full">
            <img
              src="/assets/icon.png"
              className="m-auto absolute top-0 bottom-0 left-0 right-0"
            />
          </div>
        </div>
      </DrawerTrigger>
      <DrawerContent className="px-4">
        <DrawerHeader>
          <div className="flex items-start space-x-2">
            <GantryController />
            <ToolController />
          </div>
        </DrawerHeader>
        <DrawerFooter>
          <DrawerClose>
            <Button variant="outline">Close</Button>
          </DrawerClose>
        </DrawerFooter>
      </DrawerContent>
    </Drawer>
  );
};

export default Storage;
