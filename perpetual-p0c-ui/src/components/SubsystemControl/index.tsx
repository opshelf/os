import GantryController from '../GantryController';
import FunnelController from '../FunnelController';
import ToolController from '../ToolController';

const SubsystemController = () => {
  return (
    <div className="flex flex-col w-full space-y-4">
      <GantryController />
      <FunnelController />
      <ToolController />
    </div>
  );
};

export default SubsystemController;
