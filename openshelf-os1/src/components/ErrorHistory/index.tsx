import { useStateStore } from "@/store";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { AlertCircle, Trash2, ChevronDown, ChevronUp } from "lucide-react";
import { format } from "date-fns";
import { useState } from "react";
import { WorkerError } from "@/store";

interface ErrorItemProps {
	error: WorkerError;
	index: number;
	onDismiss: (index: number) => void;
}

function ErrorItem({ error, index, onDismiss }: ErrorItemProps) {
	const [isExpanded, setIsExpanded] = useState(false);
	
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
		<div className="border-b last:border-0 py-3">
			<div className="flex items-start justify-between gap-2">
				<div className="flex-1 space-y-1">
					<div className="flex items-center gap-2">
						<AlertCircle className="h-4 w-4 text-destructive" />
						<Badge variant={getErrorTypeBadgeVariant(error.error_type)}>
							{error.error_type}
						</Badge>
						<span className="text-xs text-muted-foreground">
							{format(new Date(error.timestamp), "HH:mm:ss")}
						</span>
					</div>
					<p className="text-sm">{error.message}</p>
					{error.context && Object.keys(error.context).length > 0 && (
						<Button
							variant="ghost"
							size="sm"
							className="h-6 px-2 text-xs"
							onClick={() => setIsExpanded(!isExpanded)}
						>
							{isExpanded ? (
								<>
									<ChevronUp className="h-3 w-3 mr-1" />
									Hide Details
								</>
							) : (
								<>
									<ChevronDown className="h-3 w-3 mr-1" />
									Show Details
								</>
							)}
						</Button>
					)}
				</div>
				<Button
					variant="ghost"
					size="icon"
					className="h-8 w-8"
					onClick={() => onDismiss(index)}
				>
					<Trash2 className="h-4 w-4" />
				</Button>
			</div>
			{isExpanded && error.context && (
				<div className="mt-2 space-y-2">
					<div className="bg-muted/50 rounded-md p-3 text-xs font-mono">
						<pre className="whitespace-pre-wrap">
							{JSON.stringify(error.context, null, 2)}
						</pre>
					</div>
					{error.stack_trace && (
						<details className="text-xs">
							<summary className="cursor-pointer text-muted-foreground hover:text-foreground">
								Stack Trace
							</summary>
							<pre className="mt-2 bg-muted/50 rounded-md p-3 overflow-x-auto whitespace-pre">
								{error.stack_trace}
							</pre>
						</details>
					)}
				</div>
			)}
		</div>
	);
}

function ErrorHistory() {
	const { errors, clearErrors, dismissError, addError } = useStateStore();
	
	const emitTestError = () => {
		addError({
			timestamp: new Date().toISOString(),
			error_type: ["CommandExecutionError", "TimeoutError", "ConnectionError"][Math.floor(Math.random() * 3)],
			message: `Test error ${Math.floor(Math.random() * 1000)}: This is a sample error message for testing the error reporting system.`,
			context: {
				testId: Math.random().toString(36).substring(7),
				timestamp: Date.now(),
				source: "Manual test"
			},
			stack_trace: process.env.NODE_ENV === 'development' ? `Error: Test error
    at ErrorHistory.emitTestError (ErrorHistory.tsx:89)
    at HTMLButtonElement.onClick (ErrorHistory.tsx:105)
    at invokeFunc (react-dom.js:12859)` : undefined
		});
	};
	
	if (errors.length === 0 && process.env.NODE_ENV !== 'development') {
		return null;
	}
	
	return (
		<Card>
			<CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
				<CardTitle className="text-sm font-medium">
					Error History ({errors.length})
				</CardTitle>
				<div className="flex gap-2">
					{process.env.NODE_ENV === 'development' && (
						<Button
							variant="outline"
							size="sm"
							onClick={emitTestError}
							className="h-8 px-2 text-xs"
						>
							<AlertCircle className="h-3 w-3 mr-1" />
							Test Error
						</Button>
					)}
					<Button
						variant="ghost"
						size="sm"
						onClick={clearErrors}
						className="h-8 px-2 text-xs"
					>
						<Trash2 className="h-3 w-3 mr-1" />
						Clear All
					</Button>
				</div>
			</CardHeader>
			<CardContent>
				<ScrollArea className="h-[300px] pr-4">
					{errors.slice().reverse().map((error, index) => (
						<ErrorItem
							key={`${error.timestamp}-${index}`}
							error={error}
							index={errors.length - 1 - index}
							onDismiss={dismissError}
						/>
					))}
				</ScrollArea>
			</CardContent>
		</Card>
	);
}

export default ErrorHistory;