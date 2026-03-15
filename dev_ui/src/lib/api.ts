import { getAccessToken, loginAsDeveloper, refreshAccessToken } from './auth';

export function getApiBase(): string {
	const raw = import.meta.env.VITE_API_BASE_URL ?? `http://${location.hostname}:8000/api`;
	return raw.endsWith('/') ? raw.slice(0, -1) : raw;
}

export function getApiOrigin(): string {
	const base = new URL(getApiBase());
	return base.origin;
}

export function buildApiUrl(path: string): string {
	const base = getApiBase();
	const normalisedPath = path.startsWith('/') ? path : `/${path}`;
	return `${base}${normalisedPath}`;
}

export function buildAuthenticatedUrl(path: string): string {
	const url = buildApiUrl(path);
	const token = getAccessToken();

	if (token === null) {
		return url;
	}

	const separator = url.includes('?') ? '&' : '?';
	return `${url}${separator}token=${encodeURIComponent(token)}`;
}

export type ApiErrorDetail = {
	message?: string;
	code?: string;
	needs_home?: boolean;
	needs_home_axes?: string[];
	reason?: string;
	warnings?: string[];
	correlation_id?: string;
};

export class ApiError extends Error {
	status: number;
	detail?: ApiErrorDetail;

	constructor(message: string, status: number, detail?: ApiErrorDetail) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
	}
}

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
	const token = getAccessToken();

	const headers = new Headers(init?.headers);
	if (token !== null) {
		headers.set('Authorization', `Bearer ${token}`);
	}

	let response: Response;
	try {
		response = await fetch(buildApiUrl(path), {
			...init,
			headers
		});
	} catch (error) {
		const message = error instanceof Error ? error.message : 'network error';
		throw new ApiError(`Network error: ${message}`, 0);
	}

	if (response.status === 401) {
		try {
			await refreshAccessToken();
		} catch {
			await loginAsDeveloper();
		}
		return apiFetch<T>(path, init);
	}

	if (response.ok === false) {
		const { message, detail } = await readErrorDetail(response);
		const finalMessage = formatErrorMessage(message, detail);
		throw new ApiError(finalMessage, response.status, detail);
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
		throw new Error('failed to parse response body as json');
	}
}

function formatErrorMessage(message: string, detail?: ApiErrorDetail): string {
	let output = message;
	if (detail?.reason && detail.reason !== message) {
		output = `${output} (${detail.reason})`;
	}
	if (detail?.needs_home) {
		const lower = output.toLowerCase();
		if (!lower.includes('home')) {
			output = `${output} (home required)`;
		}
	}
	return output;
}

async function readErrorDetail(
	response: Response
): Promise<{ message: string; detail?: ApiErrorDetail }> {
	const fallback = `${response.status} ${response.statusText}`;
	let text = '';

	try {
		text = await response.text();
	} catch {
		return { message: fallback };
	}

	if (text.length === 0) {
		return { message: fallback };
	}

	try {
		const payload = JSON.parse(text);
		if (payload && typeof payload === 'object') {
			if ('detail' in payload) {
				const detail = (payload as { detail?: unknown }).detail;
				if (typeof detail === 'string') {
					return { message: detail, detail: { message: detail } };
				}
				if (detail && typeof detail === 'object') {
					const detailObject = detail as ApiErrorDetail;
					const message =
						typeof detailObject.message === 'string'
							? detailObject.message
							: JSON.stringify(detailObject);
					return { message, detail: detailObject };
				}
			}

			return { message: JSON.stringify(payload) };
		}
	} catch {
		return { message: text };
	}

	return { message: text };
}

export interface Position {
	x: number;
	y: number;
	z_offset: number;
	slide_extension: number;
	warnings?: string[];
}

export interface CalibrationStatus {
	needs_home: boolean;
	needs_home_reason?: string | null;
	needs_home_axes?: string[];
}

export interface StorageGrid {
	left_module: Shelf[];
	right_module: Shelf[];
}

export interface Shelf {
	shelf_idx: number;
	shelf_height: number;
	shelf_width: number;
	cells: Cell[];
	divider_width: number;
}

export interface Cell {
	cell_idx: number;
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
	return apiFetch<StorageGrid>(`/storage/grid?_t=${timestamp}`);
}

export async function getCalibrationStatus(): Promise<CalibrationStatus> {
	return apiFetch<CalibrationStatus>('/calibration/status');
}

export async function homeToolMotors(): Promise<CalibrationStatus> {
	return apiFetch<CalibrationStatus>('/calibration/home_tool_motors', {
		method: 'POST'
	});
}

export async function moveToCell(
	side: string,
	shelfIdx: number,
	cellIdx: number
): Promise<Position> {
	return apiFetch<Position>('/calibration/move', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
	});
}

export async function jogAxis(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<Position> {
	return apiFetch<Position>('/calibration/jog', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ axis, delta })
	});
}

export interface LaserReading {
	distance_mm: number;
}

export async function getLaserReading(): Promise<LaserReading> {
	return apiFetch<LaserReading>('/calibration/laser/read');
}

export async function testGripper(side: string, shelfIdx: number, cellIdx: number): Promise<void> {
	await apiFetch('/calibration/test', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
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
		z_offset: zOffset
	};

	if (status !== undefined) {
		payload.status = status;
	}

	await apiFetch('/calibration/save', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	});
}

export interface CalibrationScanBounds {
	x_min: number;
	x_max: number;
	y_min: number;
	y_max: number;
}

export interface CalibrationScanDetectedCell {
	shelf_idx: number;
	cell_idx: number;
	center_x_mm: number;
	center_y_mm: number;
	tool_x_mm: number;
	tool_y_mm: number;
	width_mm: number;
	height_mm: number;
	point_count: number;
	bbox_x_min: number;
	bbox_x_max: number;
	bbox_y_min: number;
	bbox_y_max: number;
}

export interface CalibrationScanDetectedShelf {
	shelf_idx: number;
	center_y_mm: number;
	shelf_height_mm: number;
	shelf_width_mm: number;
	cells: CalibrationScanDetectedCell[];
}

export interface CalibrationScanResult {
	run_id: string;
	side: string;
	bounds: CalibrationScanBounds;
	step_mm: number;
	samples_per_point: number;
	settle_ms: number;
	wall_depth_mm: number;
	wall_depth_tolerance_mm: number;
	expected_hole_width_mm: number;
	expected_hole_height_mm: number;
	min_vertical_wall_coverage_mm: number;
	min_horizontal_wall_coverage_mm: number;
	total_points: number;
	valid_points: number;
	raw_scan_path: string;
	metadata_path: string;
	shelves: CalibrationScanDetectedShelf[];
	created_at: string;
}

export interface CalibrationScanStatus {
	state: 'idle' | 'running' | 'completed' | 'failed' | 'cancelled';
	run_id: string | null;
	side: 'left_module' | 'right_module' | null;
	bounds: CalibrationScanBounds | null;
	total_points: number;
	completed_points: number;
	current_x_mm: number | null;
	current_y_mm: number | null;
	current_depth_mm: number | null;
	error: string | null;
	result: CalibrationScanResult | null;
}

export interface StartCalibrationScanParams {
	side: 'left_module' | 'right_module';
	xMin?: number;
	xMax?: number;
	yMin?: number;
	yMax?: number;
	stageExtensionMm?: number;
	apply: boolean;
}

export async function startCalibrationScan(
	params: StartCalibrationScanParams
): Promise<CalibrationScanStatus> {
	const payload: Record<string, unknown> = {
		side: params.side,
		apply: params.apply
	};

	if (params.xMin !== undefined) {
		payload.x_min = params.xMin;
	}

	if (params.xMax !== undefined) {
		payload.x_max = params.xMax;
	}

	if (params.yMin !== undefined) {
		payload.y_min = params.yMin;
	}

	if (params.yMax !== undefined) {
		payload.y_max = params.yMax;
	}

	if (params.stageExtensionMm !== undefined) {
		payload.stage_extension_mm = params.stageExtensionMm;
	}

	if (params.stageExtensionMm !== undefined) {
		payload.stage_extension_mm = params.stageExtensionMm;
	}

	return apiFetch<CalibrationScanStatus>('/calibration/scan', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	});
}

export async function getCalibrationScanStatus(): Promise<CalibrationScanStatus> {
	return apiFetch<CalibrationScanStatus>('/calibration/scan/status');
}

export async function abortCalibrationScan(): Promise<CalibrationScanStatus> {
	return apiFetch<CalibrationScanStatus>('/calibration/scan/abort', {
		method: 'POST'
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
	return apiFetch<ScaleReading>('/robot/debug/scale');
}

export async function getScaleCalibrationStatus(): Promise<ScaleCalibrationStatus> {
	return apiFetch<ScaleCalibrationStatus>('/robot/debug/scale/calibration/status');
}

export async function getScaleCalibrationParams(): Promise<ScaleCalibrationParams> {
	return apiFetch<ScaleCalibrationParams>('/robot/debug/scale/calibration/params');
}

export async function setScaleCalibrationParams(
	fullScale: number,
	calibrationWeight: number,
	precision: number,
	division: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/robot/debug/scale/calibration/params', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			full_scale: fullScale,
			calibration_weight: calibrationWeight,
			precision,
			division
		})
	});
}

export async function calibrateScaleZero(): Promise<ScaleCalibrationResult> {
	return apiFetch<ScaleCalibrationResult>('/robot/debug/scale/calibration/zero', {
		method: 'POST'
	});
}

export async function calibrateScaleWeight(): Promise<ScaleCalibrationResult> {
	return apiFetch<ScaleCalibrationResult>('/robot/debug/scale/calibration/weight', {
		method: 'POST'
	});
}

export async function tareScale(): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/robot/debug/scale/calibration/tare', {
		method: 'POST'
	});
}

export interface ScannerStatus {
	connected: boolean;
	enabled: boolean;
}

export async function getScannerStatus(): Promise<ScannerStatus> {
	return apiFetch<ScannerStatus>('/robot/debug/scanner/status');
}

export async function sendScannerScan(code: string): Promise<{ message: string }> {
	return apiFetch<{ message: string }>('/robot/debug/scanner/scan', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ code })
	});
}

export interface MotorList {
	io: string[];
	tool: string[];
}

export async function getMotors(): Promise<MotorList> {
	return apiFetch<MotorList>('/robot/debug/motors');
}

export async function simulateMotorCrash(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/crash`, {
		method: 'POST'
	});
}

export async function homeMotor(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/home`, {
		method: 'POST'
	});
}

export interface MotorProbeResult {
	motor_name: string;
	responsive: boolean;
	position_mm: number | null;
	error: string | null;
}

export interface MotorProbeResponse {
	results: MotorProbeResult[];
}

export async function probeMotors(): Promise<MotorProbeResponse> {
	return apiFetch<MotorProbeResponse>('/robot/debug/motors/probe', {
		method: 'POST'
	});
}

export async function moveMotor(
	motorName: string,
	positionMm: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/move`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ position_mm: positionMm })
	});
}

export type GripperState = 'grip' | 'release';

export async function controlGripper(state: GripperState): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/gripper?state=${state}`, {
		method: 'POST'
	});
}

export type DoorAction = 'open' | 'close' | 'home';

export async function controlDoor(action: DoorAction): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/door?action=${action}`, {
		method: 'POST'
	});
}

export interface IOBoxPosition {
	pickup_low_x: number;
	pickup_low_y: number;
	pickup_high_x: number;
	pickup_high_y: number;
	z_extension: number;
	stage_extension: number;
	warnings?: string[];
}

export async function getIOBoxCalibration(): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>('/calibration/iobox');
}

export async function moveToIOBoxPosition(position: 'low' | 'high'): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>(`/calibration/iobox/move?position=${position}`, {
		method: 'POST'
	});
}

export async function jogIOBoxAxis(
	position: 'low' | 'high',
	axis: 'x' | 'y' | 'z' | 'stage',
	delta: number
): Promise<IOBoxPosition> {
	return apiFetch<IOBoxPosition>('/calibration/iobox/jog', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ position, axis, delta })
	});
}

export async function testIOBoxSequence(
	withGripper: boolean = false
): Promise<{ success: boolean }> {
	const params = new URLSearchParams();

	if (withGripper === true) {
		params.set('with_gripper', 'true');
	}

	const query = params.toString();
	const url = `/calibration/iobox/test${query !== '' ? `?${query}` : ''}`;

	return apiFetch<{ success: boolean }>(url, {
		method: 'POST'
	});
}

export async function saveIOBoxCalibration(params: IOBoxPosition): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/calibration/iobox/save', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(params)
	});
}

export interface RotationAngles {
	front: number;
	back: number;
}

export async function getRotationAngles(): Promise<RotationAngles> {
	return apiFetch<RotationAngles>('/calibration/rotation');
}

export async function rotateToAngle(angle: number): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/calibration/rotation/rotate?angle=${angle}`, {
		method: 'POST'
	});
}

export async function saveRotationAngles(
	front: number,
	back: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/calibration/rotation/save', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ front, back })
	});
}

export interface ShelfStageExtension {
	front: number;
	back: number;
}

export async function getShelfStageExtension(): Promise<ShelfStageExtension> {
	return apiFetch<ShelfStageExtension>('/calibration/shelf');
}

export async function saveShelfStageExtension(
	front: number,
	back: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/calibration/shelf/save', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ front, back })
	});
}

export async function moveStage(
	position: number
): Promise<{ position: number; warnings?: string[] }> {
	return apiFetch<{ position: number; warnings?: string[] }>('/calibration/stage/move', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ position })
	});
}

export async function runCycleTest(
	side: string,
	shelfIdx: number,
	cellIdx: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/calibration/cycle_test', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
	});
}

export type CellIdentifier = {
	side: string;
	shelf_idx: number;
	cell_idx: number;
};

export const CELL_STATUSES = ['available', 'occupied', 'maintenance', 'disabled'] as const;

export async function updateCellsBatchStatus(
	cells: CellIdentifier[],
	status: string
): Promise<unknown[]> {
	return apiFetch<unknown[]>('/storage/locations/batch-status', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ cells, status })
	});
}
