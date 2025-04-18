//import { BoxIcon, JoystickIcon, SettingsIcon } from 'lucide-react';
import {
  BotIcon,
  BoxIcon,
  DatabaseIcon,
  // 	JoystickIcon,
  // 	SettingsIcon,
  // 	Move3DIcon,
} from "lucide-react";
import { Link } from "wouter";
import {
  Tooltip,
  TooltipProvider,
  TooltipContent,
  TooltipTrigger,
} from "../ui/tooltip";

const Header = () => {
  return (
    <div className="flex w-full items-center py-4">
      <TooltipProvider>
        <div className="flex grow justify-end space-x-8">
          <Link to="/storage-designer">
            <Tooltip>
              <TooltipTrigger>
                <BoxIcon className="w-8 h-8" />
              </TooltipTrigger>
              <TooltipContent>
                <p>Storage Designer</p>
              </TooltipContent>
            </Tooltip>
          </Link>
          <Link to="/robot">
            <Tooltip>
              <TooltipTrigger>
                <BotIcon className="w-8 h-8" />
              </TooltipTrigger>
              <TooltipContent>
                <p>Robot Controller</p>
              </TooltipContent>
            </Tooltip>
          </Link>
          <Link to="/inventory">
            <Tooltip>
              <TooltipTrigger>
                <DatabaseIcon className="w-8 h-8" />
              </TooltipTrigger>
              <TooltipContent>
                <p>Inventory</p>
              </TooltipContent>
            </Tooltip>
          </Link>
          {/*
					<Link to="/subsystem">
						<Tooltip>
							<TooltipTrigger>
								<JoystickIcon className="w-8 h-8" />
							</TooltipTrigger>
							<TooltipContent>
								<p>Subsystem Controller</p>
							</TooltipContent>
						</Tooltip>
					</Link>
					<Link to="/settings">
						<Tooltip>
							<TooltipTrigger>
								<SettingsIcon className="w-8 h-8" />
							</TooltipTrigger>
							<TooltipContent>
								<p>Robot Configuration</p>
							</TooltipContent>
						</Tooltip>
					</Link>
					<Link to="/move">
						<Tooltip>
							<TooltipTrigger>
								<Move3DIcon className="w-8 h-8" />
							</TooltipTrigger>
							<TooltipContent>
								<p>Axis Manipulation</p>
							</TooltipContent>
						</Tooltip>
					</Link>
					*/}
        </div>
      </TooltipProvider>
    </div>
  );
};

export default Header;
