import { cn } from "@/lib/utils";

interface StatusIndicatorProps {
	connected: boolean;
	className?: string;
}

const StatusIndicator = ({ connected, className }: StatusIndicatorProps) => {
	return (
		<div className={cn("flex items-center gap-2", className)}>
			<div
				className={cn(
					"h-3 w-3 rounded-full",
					connected ? "bg-green-500" : "bg-red-500",
					connected && "animate-pulse"
				)}
			/>
			<span className="text-sm text-muted-foreground">
				{connected ? "Connected" : "Disconnected"}
			</span>
		</div>
	);
};

export default StatusIndicator;