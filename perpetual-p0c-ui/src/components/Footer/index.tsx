import { useStateStore } from '@/store';
import { CircleOff, CirclePlay } from 'lucide-react';

const Footer = () => {
  const { robotStatus } = useStateStore();

  return (
    <div className="flex w-full sticky bottom-0 left-0 bg-white py-1.5 px-2 space-x-2">
      <div className="flex w-full h-full relative">
        {robotStatus !== null ? (
          <div className="flex items-center">
            <div className="w-4 h-4 rounded-[50%] bg-green-300 mr-2" />
            Robot Connected
          </div>
        ) : (
          <div className="flex items-center">
            <div className="w-4 h-4 rounded-[50%] bg-red-300 mr-2" />
            Robot Disconnected
          </div>
        )}
        {robotStatus?.current_command && (
          <div className="flex items-center justify-center absolute left-1/2 transform -translate-x-1/2">
            <p className="font-semibold mr-2">Current Command:</p>
            <p>Homing</p>
          </div>
        )}

        {robotStatus?.running ? (
          <div className="flex items-center absolute right-0">
            <CirclePlay className="w-4 h-4 mr-2" />
            Robot Running
          </div>
        ) : (
          <div className="flex items-center absolute right-0">
            <CircleOff className="w-4 h-4 mr-2" />
            Robot Stopped
          </div>
        )}
      </div>
    </div>
  );
};

export default Footer;
