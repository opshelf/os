import { RobotCommands } from "@/domain/commands/robot";
import { 
	HomeIcon, 
	MoveIcon, 
	PackageIcon, 
	BoxIcon,
	ActivityIcon,
	CircleIcon,
	PlayIcon,
	PauseIcon
} from "lucide-react";
import { cn } from "@/lib/utils";

interface CommandDisplayProps {
	command: RobotCommands | null;
	isRunning?: boolean;
	className?: string;
}

const getCommandIcon = (command: RobotCommands | null) => {
	if (command === null) return <CircleIcon className="h-4 w-4" />;
	
	switch (command.command) {
		case "home":
			return <HomeIcon className="h-4 w-4" />;
		case "move":
		case "unsafe_move":
			return <MoveIcon className="h-4 w-4" />;
		case "retrieve_item":
			return <PackageIcon className="h-4 w-4" />;
		case "store_item":
			return <BoxIcon className="h-4 w-4" />;
		case "grab_cell":
		case "place_cell":
			return <PackageIcon className="h-4 w-4" />;
		case "initialize":
			return <ActivityIcon className="h-4 w-4" />;
		case "stop":
			return <PauseIcon className="h-4 w-4" />;
		default:
			return <PlayIcon className="h-4 w-4" />;
	}
};

const getCommandLabel = (command: RobotCommands | null) => {
	if (command === null) return "Idle";
	
	switch (command.command) {
		case "home":
			return "Homing";
		case "move":
			return "Moving";
		case "unsafe_move":
			return "Unsafe Move";
		case "retrieve_item":
			return "Retrieving Item";
		case "store_item":
			return "Storing Item";
		case "grab_cell":
			return "Grabbing Cell";
		case "place_cell":
			return "Placing Cell";
		case "initialize":
			return "Initializing";
		case "stop":
			return "Stopping";
		default:
			return command.command;
	}
};

const CommandDisplay = ({ command, isRunning = false, className }: CommandDisplayProps) => {
	return (
		<div className={cn("flex items-center gap-2", className)}>
			<div className="flex items-center gap-1">
				{isRunning && command !== null && (
					<ActivityIcon className="h-4 w-4 animate-pulse text-blue-500" />
				)}
				{getCommandIcon(command)}
			</div>
			<span className="text-sm">
				{getCommandLabel(command)}
			</span>
		</div>
	);
};

export default CommandDisplay;