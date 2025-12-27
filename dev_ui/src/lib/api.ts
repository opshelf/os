import { getAccessToken, loginAsDeveloper, refreshAccessToken } from "./auth";

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

export function buildAuthenticatedUrl(path: string): string {
	const url = buildApiUrl(path);
	const token = getAccessToken();

	if (token === null) {
		return url;
	}

	const separator = url.includes("?") ? "&" : "?";
	return `${url}${separator}token=${encodeURIComponent(token)}`;
}

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
	const token = getAccessToken();

	const headers = new Headers(init?.headers);
	if (token !== null) {
		headers.set("Authorization", `Bearer ${token}`);
	}

	const response = await fetch(buildApiUrl(path), {
		...init,
		headers,
	});

	if (response.status === 401) {
		try {
			await refreshAccessToken();
		} catch {
			await loginAsDeveloper();
		}
		return apiFetch<T>(path, init);
	}

	if (response.ok === false) {
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
	slide_extension: number;
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
	const timestamp = Date.now();
	return apiFetch<StorageGrid>(`/calibration/storage?_t=${timestamp}`);
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

export async function jogAxis(axis: "x" | "y" | "z" | "stage", delta: number): Promise<Position> {
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
	zOffset: number,
	status?: string
): Promise<void> {

	const payload: Record<string, unknown> = {
		side,
		shelf_idx: shelfIdx,
		cell_idx: cellIdx,
		x,
		y,
		z_offset: zOffset,
	};

	if (status !== undefined) {
		payload.status = status;
	}

	await apiFetch("/calibration/save", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(payload),
	});
}

export interface ScaleStatus {
	stable: boolean;
	zero: boolean;
	overload: boolean;
	valid: boolean;
	raw: number;
}

export interface ScaleReading {
	weight_grams: number;
	decimal_position: number;
	status: ScaleStatus;
	raw_registers: {
		weight_hi: number;
		weight_lo: number;
		decimals: number;
		status: number;
	};
}

export interface ScaleCalibrationStatus {
	zero_ok: boolean;
	weight_ok: boolean;
	raw: number;
}

export interface ScaleCalibrationParams {
	full_scale: number;
	calibration_weight: number;
	precision: number;
	division: number;
}

export interface ScaleCalibrationResult {
	success: boolean;
	error?: string;
	status?: ScaleCalibrationStatus;
}

export async function getScaleReading(): Promise<ScaleReading> {
	return apiFetch<ScaleReading>("/robot/debug/scale");
}

export async function getScaleCalibrationStatus(): Promise<ScaleCalibrationStatus> {
	return apiFetch<ScaleCalibrationStatus>("/robot/debug/scale/calibration/status");
}

export async function getScaleCalibrationParams(): Promise<ScaleCalibrationParams> {
	return apiFetch<ScaleCalibrationParams>("/robot/debug/scale/calibration/params");
}

export async function setScaleCalibrationParams(
	fullScale: number,
	calibrationWeight: number,
	precision: number,
	division: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/robot/debug/scale/calibration/params", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({
			full_scale: fullScale,
			calibration_weight: calibrationWeight,
			precision,
			division,
		}),
	});
}

export async function calibrateScaleZero(): Promise<ScaleCalibrationResult> {
	return apiFetch<ScaleCalibrationResult>("/robot/debug/scale/calibration/zero", {
		method: "POST",
	});
}

export async function calibrateScaleWeight(): Promise<ScaleCalibrationResult> {
	return apiFetch<ScaleCalibrationResult>("/robot/debug/scale/calibration/weight", {
		method: "POST",
	});
}

export async function tareScale(): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/robot/debug/scale/calibration/tare", {
		method: "POST",
	});
}

export interface ScannerStatus {
	connected: boolean;
	enabled: boolean;
}

export async function getScannerStatus(): Promise<ScannerStatus> {
	return apiFetch<ScannerStatus>("/robot/debug/scanner/status");
}

export async function sendScannerScan(code: string): Promise<{ message: string }> {
	return apiFetch<{ message: string }>("/robot/debug/scanner/scan", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ code }),
	});
}

export interface MotorList {
	io: string[];
	tool: string[];
}

export async function getMotors(): Promise<MotorList> {
	return apiFetch<MotorList>("/robot/debug/motors");
}

export async function simulateMotorCrash(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/crash`, {
		method: "POST",
	});
}

export async function homeMotor(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/home`, {
		method: "POST",
	});
}

export type GripperState = "grip" | "release";

export async function controlGripper(state: GripperState): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/gripper?state=${state}`, {
		method: "POST",
	});
}

export type DoorAction = "open" | "close" | "home";

export async function controlDoor(action: DoorAction): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/door?action=${action}`, {
		method: "POST",
	});
}

export interface IOBoxPosition {
	pickup_low_x: number;
	pickup_low_y: number;
	pickup_high_x: number;
	pickup_high_y: number;
	z_extension: number;
	stage_extension: number;
}

export async function getIOBoxCalibration(): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>("/calibration/iobox");
}

export async function moveToIOBoxPosition(position: "low" | "high"): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>(`/calibration/iobox/move?position=${position}`, {
		method: "POST",
	});
}

export async function jogIOBoxAxis(
	position: "low" | "high",
	axis: "x" | "y" | "z" | "stage",
	delta: number
): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>("/calibration/iobox/jog", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ position, axis, delta }),
	});
}

export async function testIOBoxSequence(withGripper: boolean = false): Promise<{ success: boolean }> {
	const params = new URLSearchParams();

	if (withGripper === true) {
		params.set("with_gripper", "true");
	}

	const query = params.toString();
	const url = `/calibration/iobox/test${query !== "" ? `?${query}` : ""}`;

	return apiFetch<{ success: boolean }>(url, {
		method: "POST",
	});
}

export async function saveIOBoxCalibration(params: IOBoxPosition): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/calibration/iobox/save", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(params),
	});
}

export interface RotationAngles {
	front: number;
	back: number;
}

export async function getRotationAngles(): Promise<RotationAngles> {
	return apiFetch<RotationAngles>("/calibration/rotation");
}

export async function rotateToAngle(angle: number): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/calibration/rotation/rotate?angle=${angle}`, {
		method: "POST",
	});
}

export async function saveRotationAngles(front: number, back: number): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/calibration/rotation/save", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ front, back }),
	});
}

export interface ShelfStageExtension {
	front: number;
	back: number;
}

export async function getShelfStageExtension(): Promise<ShelfStageExtension> {
	return apiFetch<ShelfStageExtension>("/calibration/shelf");
}

export async function saveShelfStageExtension(front: number, back: number): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/calibration/shelf/save", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ front, back }),
	});
}

export async function runCycleTest(
	side: string,
	shelfIdx: number,
	cellIdx: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/calibration/cycle_test", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx }),
	});
}

export async function abortCycleTest(): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>("/calibration/abort", {
		method: "POST",
	});
}
