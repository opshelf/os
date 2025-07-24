import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { ListIcon } from "lucide-react";

interface QueueBadgeProps {
	count: number;
	onClick?: () => void;
	className?: string;
}

const QueueBadge = ({ count, onClick, className }: QueueBadgeProps) => {
	return (
		<Badge 
			variant="outline" 
			className={cn(
				"cursor-pointer transition-all hover:scale-105",
				className
			)}
			onClick={onClick}
		>
			<ListIcon className="h-3 w-3 mr-1" />
			{count === 0 ? "Queue empty" : `${count} queued`}
		</Badge>
	);
};

export default QueueBadge;