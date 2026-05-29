<script lang="ts">
	import { browser } from '$app/environment';
	import {
		getScaleReading,
		getScaleCalibrationStatus,
		getScaleCalibrationParams,
		setScaleCalibrationParams,
		calibrateScaleZero,
		calibrateScaleWeight,
		tareScale,
		type ScaleReading,
		type ScaleCalibrationStatus,
		type ScaleCalibrationParams
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	type ScaleView = 'status' | 'calibration';

	let { selectedView }: { selectedView: ScaleView } = $props();

	let reading = $state<ScaleReading | null>(null);
	let calibrationStatus = $state<ScaleCalibrationStatus | null>(null);
	let calibrationParams = $state<ScaleCalibrationParams | null>(null);

	let readingError = $state('');
	let calibrationError = $state('');
	let message = $state('');

	let isLoading = $state(true);
	let isRefreshing = $state(false);
	let isCalibrating = $state(false);
	let isSavingParams = $state(false);
	let isTaring = $state(false);

	let fullScale = $state(6000);
	let calibrationWeight = $state(500);
	let precision = $state(1);
	let division = $state(1);

	async function fetchReading(): Promise<void> {
		try {
			reading = await getScaleReading();
			readingError = '';
		} catch (err) {
			readingError = err instanceof Error ? err.message : 'failed to read scale';
		}
	}

	async function fetchCalibrationStatus(): Promise<void> {
		try {
			calibrationStatus = await getScaleCalibrationStatus();
			calibrationError = '';
		} catch (err) {
			calibrationError = err instanceof Error ? err.message : 'failed to read calibration status';
		}
	}

	async function fetchCalibrationParams(): Promise<void> {
		try {
			calibrationParams = await getScaleCalibrationParams();
			calibrationError = '';
			if (calibrationParams !== null) {
				fullScale = calibrationParams.full_scale;
				calibrationWeight = calibrationParams.calibration_weight;
				precision = calibrationParams.precision;
				division = calibrationParams.division < 1 ? 1 : calibrationParams.division;
			}
		} catch (err) {
			calibrationError = err instanceof Error ? err.message : 'failed to read calibration params';
		}
	}

	async function didClickSaveParams(): Promise<void> {
		isSavingParams = true;
		calibrationError = '';
		message = '';

		try {
			const result = await setScaleCalibrationParams(
				fullScale,
				calibrationWeight,
				precision,
				division
			);
			if (result.success === true) {
				message = 'calibration parameters saved';
				await fetchCalibrationParams();
			} else {
				calibrationError = 'failed to save calibration parameters';
			}
		} catch (err) {
			calibrationError =
				err instanceof Error ? err.message : 'failed to save calibration parameters';
		} finally {
			isSavingParams = false;
		}
	}

	async function didClickCalibrateZero(): Promise<void> {
		isCalibrating = true;
		calibrationError = '';
		message = '';

		try {
			const result = await calibrateScaleZero();
			if (result.success === true) {
				message = 'zero calibration successful';
			} else {
				calibrationError = result.error ?? 'zero calibration failed';
			}
			await fetchCalibrationStatus();
		} catch (err) {
			calibrationError = err instanceof Error ? err.message : 'failed to calibrate zero';
		} finally {
			isCalibrating = false;
		}
	}

	async function didClickCalibrateWeight(): Promise<void> {
		isCalibrating = true;
		calibrationError = '';
		message = '';

		try {
			const result = await calibrateScaleWeight();
			if (result.success === true) {
				message = 'weight calibration successful';
			} else {
				calibrationError = result.error ?? 'weight calibration failed';
			}
			await fetchCalibrationStatus();
		} catch (err) {
			calibrationError = err instanceof Error ? err.message : 'failed to calibrate weight';
		} finally {
			isCalibrating = false;
		}
	}

	async function didClickTare(): Promise<void> {
		isTaring = true;
		calibrationError = '';
		message = '';

		try {
			const result = await tareScale();
			if (result.success === true) {
				message = 'tare successful';
				await fetchReading();
			} else {
				calibrationError = 'tare failed';
			}
		} catch (err) {
			calibrationError = err instanceof Error ? err.message : 'failed to tare';
		} finally {
			isTaring = false;
		}
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		calibrationError = '';
		readingError = '';

		try {
			if (selectedView === 'status') {
				await fetchReading();
			} else {
				await fetchCalibrationStatus();
				await fetchCalibrationParams();
			}
		} finally {
			isRefreshing = false;
		}
	}

	function formatWeight(grams: number): string {
		return grams.toFixed(1);
	}

	function formatHex(value: number): string {
		return '0x' + value.toString(16).toUpperCase().padStart(4, '0');
	}

	async function loadSelectedView(view: ScaleView): Promise<void> {
		isLoading = true;
		message = '';

		try {
			if (view === 'status') {
				await fetchReading();
			} else {
				await fetchCalibrationStatus();
				await fetchCalibrationParams();
			}
		} finally {
			isLoading = false;
		}
	}

	$effect(() => {
		if (browser === false) {
			return;
		}

		void loadSelectedView(selectedView);
	});
</script>

{#if isLoading === true}
	<p>loading scale data...</p>
{:else if selectedView === 'status'}
	<table style="margin-bottom: 1.5em">
		<thead>
			<tr>
				<th colspan="2">scale reading</th>
			</tr>
		</thead>
		<tbody>
			{#if reading !== null}
				<tr><td>weight</td><td>{formatWeight(reading.weight_grams)} g</td></tr>
				<tr><td>stable</td><td class:status-ok={reading.status.stable === true} class:status-error={reading.status.stable === false}>{reading.status.stable ? 'yes' : 'no'}</td></tr>
				<tr><td>valid</td><td class:status-ok={reading.status.valid === true} class:status-error={reading.status.valid === false}>{reading.status.valid ? 'yes' : 'no'}</td></tr>
				<tr><td>zero</td><td>{reading.status.zero ? 'yes' : 'no'}</td></tr>
				<tr><td>overload</td><td class:status-error={reading.status.overload === true}>{reading.status.overload ? 'yes' : 'no'}</td></tr>
				<tr><td>decimal position</td><td>{reading.decimal_position}</td></tr>
			{:else if readingError === ''}
				<tr><td colspan="2">loading...</td></tr>
			{/if}
			{#if readingError !== ''}
				<tr><td colspan="2">{readingError.toUpperCase()}</td></tr>
			{/if}
		</tbody>
		<thead>
			<tr>
				<th colspan="2">raw registers</th>
			</tr>
		</thead>
		<tbody>
			{#if reading !== null}
				<tr><td>weight hi</td><td>{formatHex(reading.raw_registers.weight_hi)}</td></tr>
				<tr><td>weight lo</td><td>{formatHex(reading.raw_registers.weight_lo)}</td></tr>
				<tr><td>decimals</td><td>{reading.raw_registers.decimals}</td></tr>
				<tr><td>status</td><td>{formatHex(reading.raw_registers.status)}</td></tr>
			{:else if readingError === ''}
				<tr><td colspan="2">-</td></tr>
			{/if}
			{#if readingError !== ''}
				<tr><td colspan="2">{readingError.toUpperCase()}</td></tr>
			{/if}
		</tbody>
	</table>

	<div class="action-buttons">
		<button type="button" onclick={didClickRefresh} disabled={isRefreshing === true || isTaring === true}>refresh</button>
		<button type="button" onclick={didClickTare} disabled={isTaring === true}>tare</button>
	</div>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if calibrationError !== ''}
		<ErrorMessage message={calibrationError} />
	{/if}
{:else}
	<table style="margin-bottom: 1.5em">
		<thead><tr><th colspan="2">calibration status</th></tr></thead>
		<tbody>
			{#if calibrationStatus !== null}
				<tr><td>zero calibrated</td><td class:status-ok={calibrationStatus.zero_ok === true} class:status-error={calibrationStatus.zero_ok === false}>{calibrationStatus.zero_ok ? 'yes' : 'no'}</td></tr>
				<tr><td>weight calibrated</td><td class:status-ok={calibrationStatus.weight_ok === true} class:status-error={calibrationStatus.weight_ok === false}>{calibrationStatus.weight_ok ? 'yes' : 'no'}</td></tr>
				<tr><td>raw status</td><td>{formatHex(calibrationStatus.raw)}</td></tr>
			{:else if calibrationError === ''}
				<tr><td colspan="2">loading...</td></tr>
			{/if}
			{#if calibrationError !== ''}
				<tr><td colspan="2">{calibrationError.toUpperCase()}</td></tr>
			{/if}
		</tbody>
		<thead><tr><th colspan="2">calibration parameters</th></tr></thead>
		<tbody>
			<tr><td>full scale (g)</td><td><input type="number" bind:value={fullScale} min="1" max="100000" /></td></tr>
			<tr><td>calibration weight (g)</td><td><input type="number" bind:value={calibrationWeight} min="1" max="100000" /></td></tr>
			<tr><td>precision (0-3)</td><td><input type="number" bind:value={precision} min="0" max="3" /></td></tr>
			<tr><td>division</td><td><input type="number" bind:value={division} min="1" max="100" /></td></tr>
		</tbody>
	</table>

	<div class="action-buttons">
		<button type="button" onclick={didClickRefresh} disabled={isRefreshing === true || isCalibrating === true || isSavingParams === true}>refresh</button>
		<button type="button" onclick={didClickSaveParams} disabled={isSavingParams === true || isCalibrating === true}>save params</button>
	</div>

	<div class="calibration-buttons">
		<button type="button" class="calibrate-button" onclick={didClickCalibrateZero} disabled={isCalibrating === true || isSavingParams === true}>1. calibrate zero (empty scale)</button>
		<button type="button" class="calibrate-button" onclick={didClickCalibrateWeight} disabled={isCalibrating === true || isSavingParams === true}>2. calibrate weight (place {calibrationWeight}g)</button>
	</div>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if calibrationError !== ''}
		<ErrorMessage message={calibrationError} />
	{/if}
{/if}

<style>
	input[type='number'] {
		width: 8em;
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-bottom: 1.5em;
	}

	.calibration-buttons {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 1.5em;
	}
</style>
