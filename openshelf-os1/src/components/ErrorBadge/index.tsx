import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AlertCircle } from "lucide-react";

interface ErrorBadgeProps {
	count: number;
	onClick?: () => void;
	className?: string;
}

function ErrorBadge({ count, onClick, className }: ErrorBadgeProps) {
	return (
		<Button
			size="sm"
			variant="outline"
			className={cn(className)}
			onClick={onClick}
		>
			<AlertCircle className={cn("h-4 w-4 mr-1", count > 0 ? "text-destructive" : undefined)} />
			<span className={count > 0 ? "text-destructive" : undefined}>Errors</span>
			{count > 0 && (
				<span className="ml-1 text-destructive">{count}</span>
			)}
		</Button>
	);
}

export default ErrorBadge;