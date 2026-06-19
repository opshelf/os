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
	if (detail?.needs_home && detail.reason === undefined) {
		output = `${output} (home required)`;
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

export type StorageModuleSide = 'left_module' | 'right_module';

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

export async function deleteShelf(side: StorageModuleSide, shelfIdx: number): Promise<{ success: boolean }> {
	const params = new URLSearchParams({ side, shelf_idx: String(shelfIdx) });
	return apiFetch<{ success: boolean }>(`/storage/grid/shelf?${params}`, {
		method: 'DELETE'
	});
}

export async function deleteCell(side: StorageModuleSide, shelfIdx: number, cellIdx: number): Promise<{ success: boolean }> {
	const params = new URLSearchParams({ side, shelf_idx: String(shelfIdx), cell_idx: String(cellIdx) });
	return apiFetch<{ success: boolean }>(`/storage/grid/cell?${params}`, {
		method: 'DELETE'
	});
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
	side: StorageModuleSide,
	shelfIdx: number,
	cellIdx: number
): Promise<Position> {
	return apiFetch<Position>('/calibration/move', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
	});
}

export async function moveToPosition(
	side: StorageModuleSide,
	x: number,
	y: number,
	zOffset: number
): Promise<Position> {
	return apiFetch<Position>('/calibration/move/position', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, x, y, z_offset: zOffset })
	});
}

export async function jogAxis(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<Position> {
	return apiFetch<Position>('/calibration/jog', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ axis, delta })
	});
}

export async function testGripper(side: StorageModuleSide, shelfIdx: number, cellIdx: number): Promise<void> {
	await apiFetch('/calibration/test', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
	});
}

export async function runCalibrationQuickTest(
	side: StorageModuleSide,
	shelfIdx: number,
	cellIdx: number
): Promise<void> {
	await testGripper(side, shelfIdx, cellIdx);
}

export async function runCalibrationCycleTest(
	side: StorageModuleSide,
	shelfIdx: number,
	cellIdx: number
): Promise<void> {
	await apiFetch('/calibration/test/cycle', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ side, shelf_idx: shelfIdx, cell_idx: cellIdx })
	});
}

export async function storeItem(
	side: StorageModuleSide,
	shelfIdx: number,
	cellIdx: number,
	item: Record<string, unknown>
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/robot/commands', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			command: 'store_item',
			item: {
				...item,
				location: { side, shelf_idx: shelfIdx, cell_idx: cellIdx }
			}
		})
	});
}

export async function retrieveItem(
	side: StorageModuleSide,
	shelfIdx: number,
	cellIdx: number
): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>('/robot/commands', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			command: 'retrieve_item',
			location: { side, shelf_idx: shelfIdx, cell_idx: cellIdx }
		})
	});
}

export async function saveCalibration(
	side: StorageModuleSide,
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

export interface LaserAutoCalibrationShelfEdge {
	side: StorageModuleSide;
	x_mm: number;
	y_mm: number;
}

export interface LaserAutoCalibrationCandidate {
	side: StorageModuleSide;
	shelf_idx: number | null;
	x_min: number;
	x_max: number;
	y_min: number;
	y_max: number;
	row_count: number;
	wall_depth_mm: number | null;
	wall_sample_count: number;
}

export interface LaserAutoCalibrationCell {
	side: StorageModuleSide;
	shelf_idx: number;
	cell_idx: number;
	tool_x_mm: number;
	tool_y_mm: number;
	center_x_mm: number;
	center_y_mm: number;
	width_mm: number;
	height_mm: number;
	wall_depth_mm: number | null;
}

export interface LaserAutoCalibrationAngleResult {
	side: StorageModuleSide;
	alpha_deg: number;
	sample_count: number;
	capture_y_mm: number;
	rms_residual_mm: number;
	max_abs_residual_mm: number;
	line_angle_deg: number;
	previous_rotation_deg: number;
	updated_rotation_deg: number;
}

export interface LaserAutoCalibrationIOBoxResult {
	pickup_low_x: number;
	pickup_low_y: number;
	pickup_high_x: number;
	pickup_high_y: number;
	z_extension: number;
	stage_extension: number;
}

export interface LaserAutoCalibrationRequest {
	mode: 'all' | 'side' | 'shelf' | 'cell' | 'io_box';
	side?: StorageModuleSide;
	shelf_idx?: number;
	cell_idx?: number;
	include_io_box?: boolean;
	include_angle_calibration?: boolean;
}

export interface LaserAutoCalibrationSnapshot {
	state: 'idle' | 'starting' | 'running' | 'applying' | 'completed' | 'failed' | 'cancelled';
	session_id: string | null;
	run_id: string | null;
	requested_scope?: LaserAutoCalibrationRequest;
	current_side: StorageModuleSide | null;
	current_stage: string | null;
	bounds: CalibrationScanBounds | null;
	started_at: string | null;
	updated_at: string | null;
	scouted_shelf_count: number;
	left_scouted_shelf_count: number;
	right_scouted_shelf_count: number;
	candidate_count: number;
	left_candidate_count: number;
	right_candidate_count: number;
	detected_cell_count: number;
	left_detected_cell_count: number;
	right_detected_cell_count: number;
	left_stage_extension_mm: number | null;
	right_stage_extension_mm: number | null;
	shelf_edges: LaserAutoCalibrationShelfEdge[];
	shelf_candidates: LaserAutoCalibrationCandidate[];
	detected_cells: LaserAutoCalibrationCell[];
	angle_results: LaserAutoCalibrationAngleResult[];
	io_box_result: LaserAutoCalibrationIOBoxResult | null;
	stage_detail: Record<string, unknown>;
	error: string | null;
}

export function buildLaserAutoCalibrationStreamUrl(
	request: LaserAutoCalibrationRequest = {
		mode: 'all',
		include_io_box: true,
		include_angle_calibration: true
	},
	options: { subscribeOnly?: boolean } = {}
): string {
	const url = new URL(buildAuthenticatedUrl('/calibration/laser/stream'));
	if (options.subscribeOnly === true) {
		url.searchParams.set('subscribe_only', 'true');
	}

	if (request.mode === 'io_box') {
		url.searchParams.set('mode', 'io_box');
		return url.toString();
	}

	if (request.include_io_box !== undefined) {
		url.searchParams.set('include_io_box', String(request.include_io_box));
	}

	if (request.include_angle_calibration !== undefined) {
		url.searchParams.set('include_angle_calibration', String(request.include_angle_calibration));
	}

	if (request.mode === 'all') {
		return url.toString();
	}

	if (request.side !== undefined) {
		url.searchParams.set('side', request.side);
	}

	if (request.shelf_idx !== undefined) {
		url.searchParams.set('shelf_idx', String(request.shelf_idx));
	}

	if (request.cell_idx !== undefined) {
		url.searchParams.set('cell_idx', String(request.cell_idx));
	}

	return url.toString();
}

export async function getLaserAutoCalibrationStreamError(
	request: LaserAutoCalibrationRequest,
	options: { subscribeOnly?: boolean } = {}
): Promise<ApiError | null> {
	const url = buildLaserAutoCalibrationStreamUrl(request, options);

	let response: Response;
	try {
		response = await fetch(url, {
			headers: { Accept: 'text/event-stream' }
		});
	} catch {
		return null;
	}

	if (response.ok === true) {
		await response.body?.cancel();
		return null;
	}

	const { message, detail } = await readErrorDetail(response);
	return new ApiError(formatErrorMessage(message, detail), response.status, detail);
}

export async function getLaserAutoCalibrationStatus(): Promise<LaserAutoCalibrationSnapshot> {
	return apiFetch<LaserAutoCalibrationSnapshot>('/calibration/laser/status');
}

export async function abortLaserAutoCalibration(): Promise<LaserAutoCalibrationSnapshot> {
	return apiFetch<LaserAutoCalibrationSnapshot>('/calibration/laser/abort', {
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

export interface SizerCameraConfig {
	device: string | number;
	width: number;
	height: number;
	fps: number;
	flip_horizontal: boolean;
	buffer_size: number;
}

export interface SizerToFConfig {
	ftdi_url: string;
	mux_address: number;
	sensor_address: number;
	channels: number[];
	timing_budget_ms: number;
	read_timeout_s: number;
	samples: number;
	sample_interval_ms: number;
	aggregation: 'median' | 'mean' | 'nearest' | 'farthest';
	distance_mode: 1 | 2 | null;
	roi_width: number | null;
	roi_height: number | null;
	roi_center: number | null;
	preferred_sensor_index: number | null;
}

export interface SizerDetectionConfig {
	mode: 'brightness' | 'rectangle' | 'difference' | 'circle';
	roi_x: number;
	roi_y: number;
	roi_width: number;
	roi_height: number;
	diff_threshold: number;
	hsv_v_min: number;
	hsv_s_max: number;
	white_rgb_min: [number, number, number];
	min_area_px: number;
	blank_frame_path: string;
	contour_selection: 'largest' | 'center';
}

export interface SizerCalibrationParams {
	tof_blank_mm: Array<number | null>;
	mm_per_px_x: number | null;
	mm_per_px_y: number | null;
	apply_depth_compensation: boolean;
}

export interface SizerHistoryConfig {
	save_debug_images: boolean;
	debug_image_dir: string;
	max_recent_results: number;
}

export interface SizerConfig {
	enabled: boolean;
	observer_mode_enabled: boolean;
	measure_on_store: boolean;
	store_measurement_timeout_s: number;
	camera: SizerCameraConfig;
	tof: SizerToFConfig;
	detection: SizerDetectionConfig;
	calibration: SizerCalibrationParams;
	history: SizerHistoryConfig;
}

export interface SizerToFReading {
	sensor_index: number;
	sensor_channel: number;
	depth_mm: number | null;
	depth_delta_mm: number | null;
	error: string | null;
}

export interface SizerDimensions {
	width: number;
	height: number;
	length: number | null;
}

export interface SizerMeasurement {
	detected: boolean;
	mode: 'brightness' | 'rectangle' | 'difference' | 'circle';
	width_px: number | null;
	height_px: number | null;
	center_x: number | null;
	center_y: number | null;
	frame_width: number;
	frame_height: number;
	depth_mm: number | null;
	depth_delta_mm: number | null;
	sensor_index: number | null;
	sensor_channel: number | null;
	tof_readings: SizerToFReading[];
	dimensions_mm: SizerDimensions | null;
	calibration_warnings: string[];
	timestamp: string;
	duration_ms: number;
	debug_image_path: string | null;
}

export interface SizerStatus {
	enabled?: boolean;
	available?: boolean;
	running?: boolean;
	observer_mode_enabled?: boolean;
	error?: string | null;
	[key: string]: unknown;
}

export async function getSizerStatus(): Promise<SizerStatus> {
	return apiFetch<SizerStatus>('/iobox/sizer/status');
}

export async function getSizerConfig(): Promise<SizerConfig> {
	return apiFetch<SizerConfig>('/iobox/sizer/config');
}

export async function setSizerConfig(config: SizerConfig): Promise<{ success: boolean; config: SizerConfig; status: SizerStatus }> {
	return apiFetch<{ success: boolean; config: SizerConfig; status: SizerStatus }>('/iobox/sizer/config', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(config)
	});
}

export async function measureSizer(): Promise<{ success: boolean; measurement: SizerMeasurement }> {
	return apiFetch<{ success: boolean; measurement: SizerMeasurement }>('/iobox/sizer/measure', {
		method: 'POST'
	});
}

export async function calibrateSizerTofBlank(samples = 20): Promise<{ success: boolean; tof_blank_mm: Array<number | null> }> {
	return apiFetch<{ success: boolean; tof_blank_mm: Array<number | null> }>('/iobox/sizer/calibration/tof-blank', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ samples })
	});
}

export async function captureSizerCameraBlank(): Promise<{ success: boolean; blank_frame_path: string }> {
	return apiFetch<{ success: boolean; blank_frame_path: string }>('/iobox/sizer/calibration/camera-blank', {
		method: 'POST'
	});
}


export const getIOBoxSizerStatus = getSizerStatus;
export const getIOBoxSizerConfig = getSizerConfig;
export const setIOBoxSizerConfig = setSizerConfig;
export const measureIOBoxSizer = measureSizer;

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

export interface MotorInfo {
	name: string;
	motor_id: number;
}

export interface MotorList {
	io: MotorInfo[];
	tool: MotorInfo[];
}

export async function getMotors(): Promise<MotorList> {
	return apiFetch<MotorList>('/robot/debug/motors');
}

export async function simulateMotorCrash(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/crash`, {
		method: 'POST'
	});
}

export async function clearMotorAlarm(motorName: string): Promise<{ success: boolean }> {
	return apiFetch<{ success: boolean }>(`/robot/debug/motors/${motorName}/clear`, {
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
	busy: boolean;
	reason: string | null;
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

export interface WifiStatus {
	connected: boolean;
	ssid: string | null;
	signal: number | null;
	ip: string | null;
}

export interface WifiNetwork {
	ssid: string;
	signal: number;
	security: string;
	in_use: boolean;
}

export interface WifiNetworkList {
	networks: WifiNetwork[];
}

export interface WifiConnectResponse {
	success: boolean;
	ssid: string;
	message: string;
}

export async function getWifiStatus(): Promise<WifiStatus> {
	return apiFetch<WifiStatus>('/wifi/status');
}

export async function scanWifiNetworks(): Promise<WifiNetworkList> {
	return apiFetch<WifiNetworkList>('/wifi/networks');
}

export async function connectWifi(ssid: string, password: string): Promise<WifiConnectResponse> {
	return apiFetch<WifiConnectResponse>('/wifi/connect', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ ssid, password })
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

export type CabinetLatchAction = 'passive' | 'release';

export interface CabinetDoorState {
	top_closed: boolean;
	bottom_closed: boolean;
	closed: boolean;
	top_open: boolean;
	bottom_open: boolean;
	open: boolean;
}

export interface CabinetLatchState {
	top_released: boolean;
	bottom_released: boolean;
	released: boolean;
	top_engaged: boolean;
	bottom_engaged: boolean;
	engaged: boolean;
}

export interface CabinetReleaseResponse {
	success: boolean;
	action: CabinetLatchAction;
	doors: CabinetDoorState;
	latches: CabinetLatchState;
}

export interface CabinetStatusResponse {
	configured: boolean;
	doors: CabinetDoorState | null;
	latches: CabinetLatchState | null;
}

export async function getCabinetStatus(): Promise<CabinetStatusResponse> {
	return apiFetch<CabinetStatusResponse>('/cabinet/status');
}

export async function controlCabinetRelease(
	action: CabinetLatchAction
): Promise<CabinetReleaseResponse> {
	return apiFetch<CabinetReleaseResponse>(`/cabinet/release?action=${action}`, {
		method: 'POST'
	});
}

export async function acknowledgeSafety(): Promise<{ message: string }> {
	return apiFetch<{ message: string }>('/robot/safety/acknowledge', {
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

