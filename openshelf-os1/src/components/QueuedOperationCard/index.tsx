import { QueuedOperation } from "@/domain/status";
import { Card, CardContent } from "@/components/ui/card";
import CommandDisplay from "@/components/CommandDisplay";
import { ClockIcon, MapPinIcon, PackageIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface QueuedOperationCardProps {
	operation: QueuedOperation;
	index: number;
	className?: string;
}

const formatTimestamp = (timestamp: string) => {
	const date = new Date(timestamp);
	const now = new Date();
	const diff = now.getTime() - date.getTime();
	
	const minutes = Math.floor(diff / 60000);
	const seconds = Math.floor((diff % 60000) / 1000);
	
	if (minutes > 0) {
		return `${minutes}m ${seconds}s ago`;
	}
	return `${seconds}s ago`;
};

const formatLocation = (location: QueuedOperation["location"]) => {
	if (location === null) return null;
	return `${location.side === "left_module" ? "L" : "R"}${location.shelf_idx}-${location.cell_idx}`;
};

const QueuedOperationCard = ({ operation, index, className }: QueuedOperationCardProps) => {
	const location = formatLocation(operation.location);
	const hasItem = operation.item_data !== null;
	
	return (
		<Card className={cn("transition-all hover:shadow-md", className)}>
			<CardContent className="p-4">
				<div className="flex items-start justify-between mb-2">
					<div className="flex items-center gap-2">
						<span className="text-xs text-muted-foreground">#{index + 1}</span>
						<CommandDisplay command={operation.command} />
					</div>
					<div className="flex items-center gap-1 text-xs text-muted-foreground">
						<ClockIcon className="h-3 w-3" />
						{formatTimestamp(operation.timestamp)}
					</div>
				</div>
				
				<div className="flex items-center gap-4 text-sm">
					{location !== null && (
						<div className="flex items-center gap-1">
							<MapPinIcon className="h-3 w-3 text-muted-foreground" />
							<span className="font-mono">{location}</span>
						</div>
					)}
					
					{hasItem && operation.item_data && (
						<div className="flex items-center gap-1">
							<PackageIcon className="h-3 w-3 text-muted-foreground" />
							<span className="text-xs">
								{operation.item_data.dimensions.length}×
								{operation.item_data.dimensions.width}×
								{operation.item_data.dimensions.height}mm
							</span>
							{operation.item_data.weight !== undefined && (
								<span className="text-xs text-muted-foreground">
									({operation.item_data.weight}g)
								</span>
							)}
						</div>
					)}
					
					{operation.item_data?.upca && (
						<span className="text-xs font-mono text-muted-foreground">
							{operation.item_data.upca}
						</span>
					)}
				</div>
			</CardContent>
		</Card>
	);
};

export default QueuedOperationCard;