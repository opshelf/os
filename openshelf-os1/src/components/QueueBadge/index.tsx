import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ListIcon } from "lucide-react";

interface QueueBadgeProps {
	count: number;
	onClick?: () => void;
	className?: string;
}

const QueueBadge = ({ count, onClick, className }: QueueBadgeProps) => {
	return (
		<Button
			size="sm"
			variant="outline"
			className={cn(className)}
			onClick={onClick}
		>
			<ListIcon className="h-4 w-4 mr-1" />
			<span>Queue</span>
			<span className="ml-1 text-muted-foreground">
				{count === 0 ? "Empty" : count}
			</span>
		</Button>
	);
};

export default QueueBadge;