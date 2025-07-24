import { useState } from "react";
import { useStateStore } from "@/store";
import { Drawer, DrawerContent, DrawerTrigger } from "@/components/ui/drawer";
import { ScrollArea } from "@/components/ui/scroll-area";
import ErrorBadge from "@/components/ErrorBadge";
import { Badge } from "@/components/ui/badge";
import { format } from "date-fns";
import { AlertCircle } from "lucide-react";
import { WorkerError } from "@/store";

interface ErrorItemProps {
	error: WorkerError;
}

function ErrorItem({ error }: ErrorItemProps) {
	const getErrorTypeBadgeVariant = (type: string) => {
		switch (type) {
			case "CommandExecutionError":
				return "destructive";
			case "ConnectionError":
				return "secondary";
			case "TimeoutError":
				return "outline";
			default:
				return "default";
		}
	};
	
	return (
		<div className="flex items-center gap-2 text-sm py-2">
			<Badge variant={getErrorTypeBadgeVariant(error.error_type)} className="text-xs">
				{error.error_type}
			</Badge>
			<span className="truncate">{error.message}</span>
			<div className="flex-1 border-b border-dotted border-gray-300 mx-2" />
			<span className="text-xs text-muted-foreground whitespace-nowrap">
				{format(new Date(error.timestamp), "HH:mm:ss")}
			</span>
		</div>
	);
}

function ErrorDrawer() {
	const [open, setOpen] = useState(false);
	const errors = useStateStore((state) => state.errors);
	
	return (
		<Drawer open={open} onOpenChange={setOpen}>
			<DrawerTrigger asChild>
				<div>
					<ErrorBadge count={errors.length} />
				</div>
			</DrawerTrigger>
			<DrawerContent className="max-h-[85vh] min-h-[400px]">
				<ScrollArea className="h-full p-4">
					<div className="space-y-1">
						{errors.length === 0 ? (
							<div className="text-center py-12 text-muted-foreground">
								<AlertCircle className="h-12 w-12 mx-auto mb-4 opacity-20" />
								<p className="text-lg">
									No errors reported
								</p>
								<p className="text-sm mt-2">
									Errors will appear here when they occur
								</p>
							</div>
						) : (
							errors.slice().reverse().map((error, index) => (
								<ErrorItem
									key={`${error.timestamp}-${index}`}
									error={error}
								/>
							))
						)}
					</div>
				</ScrollArea>
			</DrawerContent>
		</Drawer>
	);
}

export default ErrorDrawer;