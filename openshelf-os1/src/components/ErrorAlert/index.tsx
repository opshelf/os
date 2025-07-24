import { useStateStore } from "@/store";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { AlertCircle, X } from "lucide-react";
import { format } from "date-fns";

function ErrorAlert() {
	const { errors, dismissError } = useStateStore();
	
	if (errors.length === 0) {
		return null;
	}
	
	const latestError = errors[errors.length - 1];
	
	return (
		<Alert variant="destructive" className="relative">
			<AlertCircle className="h-4 w-4" />
			<AlertTitle className="pr-8">
				{latestError.error_type}
			</AlertTitle>
			<AlertDescription>
				<div className="space-y-1">
					<p>{latestError.message}</p>
					<p className="text-xs text-muted-foreground">
						{format(new Date(latestError.timestamp), "HH:mm:ss")}
					</p>
				</div>
			</AlertDescription>
			<Button
				variant="ghost"
				size="icon"
				className="absolute top-2 right-2 h-6 w-6"
				onClick={() => dismissError(errors.length - 1)}
			>
				<X className="h-4 w-4" />
			</Button>
		</Alert>
	);
}

export default ErrorAlert;