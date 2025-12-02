export function getApiBase(): string {
	const raw = import.meta.env.VITE_API_BASE_URL ?? `http://${location.hostname}:8000/api`;
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
		throw new Error("failed to parse response body as json");
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

export interface Position {
	x: number;
	y: number;
	z_offset: number;
}

export interface StorageGrid {
	left_module: Shelf[];
	right_module: Shelf[];
}

export interface Shelf {
	shelf_height: number;
	shelf_width: number;
	cells: Cell[];
	divider_width: number;
}

export interface Cell {
	status: string;
	item: unknown | null;
	calibration_point: CalibrationPoint | null;
	dimensions: Dimensions;
	empty: boolean;
}

export interface CalibrationPoint {
	x: number;
	y: number;
	s: number;
	z_offset: number;
}

export interface Dimensions {
	length: number;
	width: number;
	height: number;
}

export async function getStorageGrid(): Promise<StorageGrid> {
	return apiFetch<StorageGrid>("/calibration/storage");
}

export async function moveToCell(
	side: string,
	shelfIdx: number,
	cellIdx: number
): Promise<Position> {
	return apiFetch<Position>("/calibration/move", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx }),
	});
}

export async function jogAxis(axis: string, delta: number): Promise<Position> {
	return apiFetch<Position>("/calibration/jog", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ axis, delta }),
	});
}

export async function testGripper(
	side: string,
	shelfIdx: number,
	cellIdx: number
): Promise<void> {
	await apiFetch("/calibration/test", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx }),
	});
}

export async function saveCalibration(
	side: string,
	shelfIdx: number,
	cellIdx: number,
	x: number,
	y: number,
	zOffset: number
): Promise<void> {
	await apiFetch("/calibration/save", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({
			side,
			shelf_idx: shelfIdx,
			cell_idx: cellIdx,
			x,
			y,
			z_offset: zOffset,
		}),
	});
}
