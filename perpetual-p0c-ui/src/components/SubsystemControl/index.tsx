import GantryController from '../GantryController';
import FunnelController from '../FunnelController';
import ToolController from '../ToolController';
import WorkstationController from '../WorkstationController';

const SubsystemController = () => {
  return (
    <div className="flex flex-col w-full space-y-4">
      <GantryController />
      <FunnelController />
      <ToolController />
      <WorkstationController />
    </div>
  );
};

export default SubsystemController;
