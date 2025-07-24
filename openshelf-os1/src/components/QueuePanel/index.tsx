import { useState } from "react";
import { useStateStore } from "@/store";
import {
	Drawer,
	DrawerContent,
	DrawerDescription,
	DrawerHeader,
	DrawerTitle,
	DrawerTrigger,
} from "@/components/ui/drawer";
import { ScrollArea } from "@/components/ui/scroll-area";
import QueueBadge from "@/components/QueueBadge";
import QueuedOperationCard from "@/components/QueuedOperationCard";
import { Separator } from "@/components/ui/separator";

const QueuePanel = () => {
	const [open, setOpen] = useState(false);
	const { robotStatus } = useStateStore();
	
	const queueLength = robotStatus?.queue_length ?? 0;
	const queuedOperations = robotStatus?.queued_operations ?? [];
	
	if (queueLength === 0) return null;
	
	return (
		<Drawer open={open} onOpenChange={setOpen}>
			<DrawerTrigger asChild>
				<div>
					<QueueBadge count={queueLength} />
				</div>
			</DrawerTrigger>
			<DrawerContent className="max-h-[85vh]">
				<DrawerHeader>
					<DrawerTitle>Queued Operations</DrawerTitle>
					<DrawerDescription>
						{queueLength} operation{queueLength !== 1 ? "s" : ""} waiting to be processed
					</DrawerDescription>
				</DrawerHeader>
				
				<Separator className="my-4" />
				
				<ScrollArea className="h-[calc(100vh-200px)]">
					<div className="space-y-3">
						{queuedOperations.length > 0 ? (
							queuedOperations.map((operation, index) => (
								<QueuedOperationCard
									key={`${operation.timestamp}-${index}`}
									operation={operation}
									index={index}
								/>
							))
						) : (
							<div className="text-center py-8 text-muted-foreground">
								<p>Queue details are loading...</p>
								<p className="text-sm mt-2">
									Total queued: {queueLength}
								</p>
							</div>
						)}
						
						{queuedOperations.length > 0 && queuedOperations.length < queueLength && (
							<div className="text-center text-sm text-muted-foreground py-4">
								Showing {queuedOperations.length} of {queueLength} operations
							</div>
						)}
					</div>
				</ScrollArea>
			</DrawerContent>
		</Drawer>
	);
};

export default QueuePanel;