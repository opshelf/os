const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export async function fetchOpenApiSchema(): Promise<any> {
	const response = await fetch(`${API_URL}/openapi.json`);

	if (response.ok === false) {
		throw new Error(
			`failed to fetch OpenAPI schema: ${response.statusText}`,
		);
	}

	return response.json();
}

export async function sendRobotCommand(command: any): Promise<void> {
	const response = await fetch(`${API_URL}/api/robot/commands`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
		},
		body: JSON.stringify(command),
	});

	if (response.ok === false) {
		const error = await response.text();
		throw new Error(`failed to send command: ${error.toLocaleLowerCase()}`);
	}
}

export function createStatusStream(): EventSource {
	return new EventSource(`${API_URL}/api/robot/status`);
}
