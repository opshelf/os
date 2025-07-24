import { useStateStore } from "@/store";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { format } from "date-fns";
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
		<div className="flex items-center gap-2 text-sm">
			<Badge variant={getErrorTypeBadgeVariant(error.error_type)} className="text-xs">
				{error.error_type}
			</Badge>
			<span className="flex-1 truncate">{error.message}</span>
			<div className="h-3 w-px bg-gray-200" />
			<span className="text-xs text-muted-foreground whitespace-nowrap">
				{format(new Date(error.timestamp), "HH:mm:ss")}
			</span>
		</div>
	);
}

function ErrorHistory() {
	const errors = useStateStore((state) => state.errors);
	
	if (errors.length === 0) {
		return null;
	}
	
	return (
		<Card>
			<CardHeader>
				<CardTitle>Errors</CardTitle>
			</CardHeader>
			<CardContent>
				<div className="space-y-1">
					{errors.slice().reverse().map((error, index) => (
						<ErrorItem
							key={`${error.timestamp}-${index}`}
							error={error}
						/>
					))}
				</div>
			</CardContent>
		</Card>
	);
}

export default ErrorHistory;