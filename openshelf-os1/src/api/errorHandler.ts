import { useStateStore } from "@/store";
import { toast } from "sonner";

export async function handleApiCall<T>(
	apiCall: () => Promise<T>,
	errorMessage: string
): Promise<T | null> {
	try {
		return await apiCall();
	} catch (error) {
		const store = useStateStore.getState();
		const errorDetails = {
			timestamp: new Date().toISOString(),
			error_type: "CommandExecutionError",
			message: errorMessage,
			context: {
				error: error instanceof Error ? error.message : String(error),
				response: error instanceof Error && 'response' in error ? (error as any).response?.data : undefined,
			},
		};
		
		store.addError(errorDetails);
		toast.error(errorMessage);
		
		return null;
	}
}