import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { ListIcon } from "lucide-react";

interface QueueBadgeProps {
	count: number;
	onClick?: () => void;
	className?: string;
}

const QueueBadge = ({ count, onClick, className }: QueueBadgeProps) => {
	if (count === 0) return null;
	
	const variant = count > 5 ? "destructive" : count > 2 ? "secondary" : "outline";
	
	return (
		<Badge 
			variant={variant} 
			className={cn(
				"cursor-pointer transition-all hover:scale-105",
				className
			)}
			onClick={onClick}
		>
			<ListIcon className="h-3 w-3 mr-1" />
			{count} queued
		</Badge>
	);
};

export default QueueBadge;