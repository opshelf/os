import {
  BotIcon,
  BoxIcon,
  DatabaseIcon,
  RulerIcon,
  SettingsIcon,
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
          <Link to="/storage">
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
          <Link to="/sizes">
            <Tooltip>
              <TooltipTrigger>
                <RulerIcon className="w-8 h-8" />
              </TooltipTrigger>
              <TooltipContent>
                <p>Sizes</p>
              </TooltipContent>
            </Tooltip>
          </Link>
          <Link to="/configuration">
            <Tooltip>
              <TooltipTrigger>
                <SettingsIcon className="w-8 h-8" />
              </TooltipTrigger>
              <TooltipContent>
                <p>Configuration</p>
              </TooltipContent>
            </Tooltip>
          </Link>
        </div>
      </TooltipProvider>
    </div>
  );
};

export default Header;
