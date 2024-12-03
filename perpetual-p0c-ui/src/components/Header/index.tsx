//import { BoxIcon, JoystickIcon, SettingsIcon } from 'lucide-react';
import { BoxIcon } from 'lucide-react';
import { Link } from 'wouter';

const Header = () => {
  return (
    <div className="flex w-full items-center py-4">
      <Link to="/">
        <img src="/assets/perpetual-logo-vector.svg" width="180" />
      </Link>
      <div className="flex grow justify-end space-x-8">
        {/*
        <Link to="/control">
          <JoystickIcon className="w-8 h-8" />
        </Link>
        */}
        <Link to="/storage-designer">
          <BoxIcon className="w-8 h-8" />
        </Link>
        {/*
        <Link to="/">
          <SettingsIcon className="w-8 h-8" />
        </Link>
        */}
      </div>
    </div>
  );
};

export default Header;
