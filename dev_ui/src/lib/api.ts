export function getApiBase(): string {
	const raw = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";
	return raw.endsWith("/") ? raw.slice(0, -1) : raw;
}

export function getApiOrigin(): string {
	const base = new URL(getApiBase());
	return base.origin;
}

export function buildApiUrl(path: string): string {
	const base = getApiBase();
	const normalisedPath = path.startsWith("/") ? path : `/${path}`;
	return `${base}${normalisedPath}`;
}

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(buildApiUrl(path), init);
	if (!response.ok) {
		const message = await safeReadError(response);
		throw new Error(message);
	}
	if (response.status === 204) {
		return undefined as T;
	}
	const text = await response.text();
	if (text.length === 0) {
		return undefined as T;
	}
	try {
		return JSON.parse(text) as T;
	} catch (error) {
		throw new Error("Failed to parse response body as JSON");
	}
}

async function safeReadError(response: Response): Promise<string> {
	try {
		const payload = await response.json();
		if (payload && typeof payload === "object" && "detail" in payload) {
			return String(payload.detail);
		}
		return JSON.stringify(payload);
	} catch {
		return `${response.status} ${response.statusText}`;
	}
}
