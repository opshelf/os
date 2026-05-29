<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		captureIOBoxSizerCameraBlank,
		calibrateIOBoxSizerToFBlank,
		getIOBoxSizerConfig,
		getIOBoxSizerMeasurements,
		getIOBoxSizerStatus,
		measureIOBoxSizer,
		reconnectIOBoxSizer,
		saveIOBoxSizerConfig,
		type IOBoxSizerConfig,
		type IOBoxSizerMeasurement,
		type IOBoxSizerMeasurementRow,
		type IOBoxSizerStatus
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let status: IOBoxSizerStatus | null = null;
	let config: IOBoxSizerConfig | null = null;
	let measurement: IOBoxSizerMeasurement | null = null;
	let measurements: IOBoxSizerMeasurementRow[] = [];

	let channelsText = '';
	let tofBlankText = '';
	let message = '';
	let error = '';

	let isLoading = true;
	let isRefreshing = false;
	let isSavingConfig = false;
	let isMeasuring = false;
	let isCalibrating = false;
	let isReconnecting = false;

	function formatBoolean(value: boolean): string {
		return value ? 'yes' : 'no';
	}

	function formatNumber(value: number | null | undefined, digits = 1): string {
		if (value === null || value === undefined) {
			return '-';
		}
		return value.toFixed(digits);
	}

	function formatDimensions(value: unknown): string {
		if (value === null || value === undefined) {
			return '-';
		}
		if (typeof value === 'object') {
			const item = value as { width?: number; height?: number; length?: number };
			if (item.width !== undefined && item.height !== undefined) {
				return `${formatNumber(item.width)} x ${formatNumber(item.height)} x ${formatNumber(item.length)}`;
			}
		}
		return JSON.stringify(value);
	}

	function syncTextInputs(): void {
		if (config === null) {
			return;
		}
		channelsText = config.tof.channels.join(', ');
		tofBlankText = config.calibration.tof_blank_mm.map((value) => value ?? '').join(', ');
	}

	function parseNumberList(value: string): number[] {
		return value
			.split(',')
			.map((part) => part.trim())
			.filter((part) => part !== '')
			.map((part) => Number(part));
	}

	function parseNullableNumberList(value: string): Array<number | null> {
		return value.split(',').map((part) => {
			const trimmed = part.trim();
			if (trimmed === '') {
				return null;
			}
			return Number(trimmed);
		});
	}

	async function fetchAll(): Promise<void> {
		status = await getIOBoxSizerStatus();
		config = await getIOBoxSizerConfig();
		const history = await getIOBoxSizerMeasurements(50, 0);
		measurements = history.measurements;
		syncTextInputs();
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		error = '';
		message = '';
		try {
			await fetchAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to refresh sizer';
		} finally {
			isRefreshing = false;
		}
	}

	async function didClickMeasure(): Promise<void> {
		isMeasuring = true;
		error = '';
		message = '';
		try {
			const result = await measureIOBoxSizer(true);
			measurement = result.measurement;
			message = result.measurement.detected ? 'measurement saved' : 'no object detected';
			await fetchAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'measurement failed';
		} finally {
			isMeasuring = false;
		}
	}

	async function didClickSaveConfig(): Promise<void> {
		if (config === null) {
			return;
		}
		isSavingConfig = true;
		error = '';
		message = '';
		try {
			config.tof.channels = parseNumberList(channelsText);
			config.calibration.tof_blank_mm = parseNullableNumberList(tofBlankText);
			const result = await saveIOBoxSizerConfig(config);
			config = result.config;
			status = result.status;
			syncTextInputs();
			message = 'sizer config saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save config';
		} finally {
			isSavingConfig = false;
		}
	}

	async function didClickCalibrateTofBlank(): Promise<void> {
		isCalibrating = true;
		error = '';
		message = '';
		try {
			const result = await calibrateIOBoxSizerToFBlank(5);
			tofBlankText = result.tof_blank_mm.map((value) => value ?? '').join(', ');
			message = 'tof blank saved';
			await fetchAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'tof blank calibration failed';
		} finally {
			isCalibrating = false;
		}
	}

	async function didClickCaptureCameraBlank(): Promise<void> {
		isCalibrating = true;
		error = '';
		message = '';
		try {
			await captureIOBoxSizerCameraBlank();
			message = 'camera blank saved';
			await fetchAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'camera blank capture failed';
		} finally {
			isCalibrating = false;
		}
	}

	async function didClickReconnect(): Promise<void> {
		isReconnecting = true;
		error = '';
		message = '';
		try {
			const result = await reconnectIOBoxSizer();
			status = result.status;
			message = 'sizer reconnected';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to reconnect sizer';
		} finally {
			isReconnecting = false;
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;
		try {
			await fetchAll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load sizer';
		} finally {
			isLoading = false;
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}
		initialLoad();
	});
</script>

{#if isLoading === true}
	<p>loading sizer data...</p>
{:else}
	<table style="margin-bottom: 1.5em">
		<thead><tr><th colspan="2">status</th></tr></thead>
		<tbody>
			{#if status !== null}
				<tr><td>enabled</td><td>{formatBoolean(status.enabled)}</td></tr>
				<tr><td>available</td><td class:status-ok={status.available} class:status-error={!status.available}>{formatBoolean(status.available)}</td></tr>
				<tr><td>observer mode</td><td>{formatBoolean(status.observer_mode_enabled)}</td></tr>
				<tr><td>measure on store</td><td>{formatBoolean(status.measure_on_store)}</td></tr>
				<tr><td>camera</td><td>{status.camera.device} {status.camera.width}x{status.camera.height}@{status.camera.fps}</td></tr>
				<tr><td>tof channels</td><td>{status.tof_channels.join(', ')}</td></tr>
				<tr><td>last error</td><td>{status.last_error ?? '-'}</td></tr>
				<tr><td>last measurement</td><td>{status.last_measurement_time ?? '-'}</td></tr>
			{/if}
		</tbody>

		<thead><tr><th colspan="2">manual measurement</th></tr></thead>
		<tbody>
			{#if measurement !== null}
				<tr><td>detected</td><td>{formatBoolean(measurement.detected)}</td></tr>
				<tr><td>size px</td><td>{formatNumber(measurement.width_px, 0)} x {formatNumber(measurement.height_px, 0)}</td></tr>
				<tr><td>center</td><td>{formatNumber(measurement.center_x)} / {formatNumber(measurement.center_y)}</td></tr>
				<tr><td>depth</td><td>{formatNumber(measurement.depth_mm)} mm delta {formatNumber(measurement.depth_delta_mm)} mm</td></tr>
				<tr><td>dimensions</td><td>{formatDimensions(measurement.dimensions_mm)}</td></tr>
				<tr><td>warnings</td><td>{measurement.calibration_warnings.join(', ') || '-'}</td></tr>
			{:else}
				<tr><td colspan="2">-</td></tr>
			{/if}
		</tbody>

		{#if config !== null}
			<thead><tr><th colspan="2">config</th></tr></thead>
			<tbody>
				<tr><td>enabled</td><td><input type="checkbox" bind:checked={config.enabled} /></td></tr>
				<tr><td>observer mode enabled</td><td><input type="checkbox" bind:checked={config.observer_mode_enabled} /></td></tr>
				<tr><td>measure on store</td><td><input type="checkbox" bind:checked={config.measure_on_store} /></td></tr>
				<tr><td>store timeout s</td><td><input type="number" bind:value={config.store_measurement_timeout_s} min="0.1" step="0.1" /></td></tr>
				<tr><td>camera device</td><td><input type="text" bind:value={config.camera.device} /></td></tr>
				<tr><td>camera width</td><td><input type="number" bind:value={config.camera.width} min="1" /></td></tr>
				<tr><td>camera height</td><td><input type="number" bind:value={config.camera.height} min="1" /></td></tr>
				<tr><td>camera fps</td><td><input type="number" bind:value={config.camera.fps} min="1" /></td></tr>
				<tr><td>flip horizontal</td><td><input type="checkbox" bind:checked={config.camera.flip_horizontal} /></td></tr>
				<tr><td>tof channels</td><td><input type="text" bind:value={channelsText} /></td></tr>
				<tr><td>roi x/y</td><td><input type="number" bind:value={config.detection.roi_x} min="0" /> <input type="number" bind:value={config.detection.roi_y} min="0" /></td></tr>
				<tr><td>roi width/height</td><td><input type="number" bind:value={config.detection.roi_width} min="1" /> <input type="number" bind:value={config.detection.roi_height} min="1" /></td></tr>
				<tr><td>detection mode</td><td><select bind:value={config.detection.mode}><option value="brightness">brightness</option><option value="rectangle">rectangle</option></select></td></tr>
				<tr><td>hsv v min / s max</td><td><input type="number" bind:value={config.detection.hsv_v_min} min="0" max="255" /> <input type="number" bind:value={config.detection.hsv_s_max} min="0" max="255" /></td></tr>
				<tr><td>diff threshold</td><td><input type="number" bind:value={config.detection.diff_threshold} min="0" max="255" /></td></tr>
				<tr><td>min area px</td><td><input type="number" bind:value={config.detection.min_area_px} min="1" /></td></tr>
				<tr><td>mm per px x/y</td><td><input type="number" bind:value={config.calibration.mm_per_px_x} min="0" step="0.001" /> <input type="number" bind:value={config.calibration.mm_per_px_y} min="0" step="0.001" /></td></tr>
				<tr><td>tof blanks</td><td><input type="text" bind:value={tofBlankText} /></td></tr>
				<tr><td>save debug images</td><td><input type="checkbox" bind:checked={config.history.save_debug_images} /></td></tr>
			</tbody>
		{/if}

		<thead><tr><th colspan="8">recent measurements</th></tr></thead>
		<tbody>
			<tr><th>time</th><th>trigger</th><th>upca</th><th>human</th><th>raw</th><th>auto mm</th><th>detected</th><th>error</th></tr>
			{#each measurements as row}
				<tr>
					<td>{new Date(row.created_at).toLocaleString()}</td>
					<td>{row.trigger}</td>
					<td>{row.upca ?? '-'}</td>
					<td>{formatDimensions(row.human_dimensions)}</td>
					<td>{formatNumber(row.measurement?.width_px, 0)} x {formatNumber(row.measurement?.height_px, 0)} / {formatNumber(row.measurement?.depth_mm)}</td>
					<td>{formatDimensions(row.measurement?.dimensions_mm)}</td>
					<td>{formatBoolean(row.detected)}</td>
					<td>{row.error_message ?? '-'}</td>
				</tr>
			{/each}
			{#if measurements.length === 0}
				<tr><td colspan="8">-</td></tr>
			{/if}
		</tbody>
	</table>

	<div class="action-buttons">
		<button type="button" on:click={didClickRefresh} disabled={isRefreshing}>refresh</button>
		<button type="button" on:click={didClickMeasure} disabled={isMeasuring}>measure now</button>
		<button type="button" on:click={didClickSaveConfig} disabled={isSavingConfig}>save config</button>
		<button type="button" on:click={didClickCalibrateTofBlank} disabled={isCalibrating}>calibrate tof blank</button>
		<button type="button" on:click={didClickCaptureCameraBlank} disabled={isCalibrating}>capture camera blank</button>
		<button type="button" on:click={didClickReconnect} disabled={isReconnecting}>reconnect sizer</button>
	</div>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}
	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	input[type='number'],
	input[type='text'],
	select {
		width: 9em;
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	input[type='text'] {
		width: 18em;
	}

	.action-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 1em;
		margin-bottom: 1.5em;
	}
</style>
