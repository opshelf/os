import { useStateStore } from "@/store";
import { Badge } from "@/components/ui/badge";
import { AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";

interface ErrorIndicatorProps {
	className?: string;
	showCount?: boolean;
}

function ErrorIndicator({ className, showCount = true }: ErrorIndicatorProps) {
	const errors = useStateStore((state) => state.errors);

	if (errors.length === 0) {
		return null;
	}

	return (
		<>
			<div className="h-4 w-px bg-gray-300" />
			<div className={cn("flex items-center gap-2", className)}>
				<AlertCircle className="h-4 w-4 text-destructive animate-pulse" />
				{showCount && (
					<Badge
						variant="destructive"
						className="h-5 min-w-5 px-1.5 text-xs"
					>
						{errors.length}
					</Badge>
				)}
			</div>
		</>
	);
}

export default ErrorIndicator;
