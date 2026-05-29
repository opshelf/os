<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { measureSizer, type SizerMeasurement } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let measurement: SizerMeasurement | null = $state(null);
	let measurementError = $state('');
	let message = $state('');
	let isMeasuring = $state(false);

	async function didClickMeasure(): Promise<void> {
		isMeasuring = true;
		measurementError = '';
		message = '';

		try {
			const result = await measureSizer(true);
			measurement = result.measurement;
			message = result.success === true ? 'measurement complete' : '';
		} catch (err) {
			measurementError = err instanceof Error ? err.message : 'failed to measure';
		} finally {
			isMeasuring = false;
		}
	}

	function formatOptionalNumber(value: number | null, digits = 2): string {
		return value === null ? '-' : value.toFixed(digits);
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		didClickMeasure();
	});
</script>


{#if measurement !== null}
	<table style="margin-bottom: 1.5em">
		<thead>
			<tr>
				<th colspan="2">measurement data</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>detected</td>
				<td class:status-ok={measurement.detected === true} class:status-error={measurement.detected === false}>
					{measurement.detected ? 'yes' : 'no'}
				</td>
			</tr>
			<tr>
				<td>mode</td>
				<td>{measurement.mode}</td>
			</tr>
			<tr>
				<td>dimensions</td>
				<td>
					{#if measurement.dimensions_mm !== null}
						{measurement.dimensions_mm.width.toFixed(2)}w × {measurement.dimensions_mm.height.toFixed(2)}h × {formatOptionalNumber(measurement.dimensions_mm.length)}l mm
					{:else}
						-
					{/if}
				</td>
			</tr>
			<tr>
				<td>bounding box</td>
				<td>{measurement.width_px ?? '-'}w × {measurement.height_px ?? '-'}h px</td>
			</tr>
			<tr>
				<td>center</td>
				<td>{formatOptionalNumber(measurement.center_x, 1)}, {formatOptionalNumber(measurement.center_y, 1)}</td>
			</tr>
			<tr>
				<td>frame</td>
				<td>{measurement.frame_width} × {measurement.frame_height} px</td>
			</tr>
			<tr>
				<td>depth</td>
				<td>{formatOptionalNumber(measurement.depth_mm)} mm</td>
			</tr>
			<tr>
				<td>depth delta</td>
				<td>{formatOptionalNumber(measurement.depth_delta_mm)} mm</td>
			</tr>
			<tr>
				<td>sensor</td>
				<td>{measurement.sensor_index ?? '-'} / channel {measurement.sensor_channel ?? '-'}</td>
			</tr>
			<tr>
				<td>duration</td>
				<td>{measurement.duration_ms} ms</td>
			</tr>
			<tr>
				<td>timestamp</td>
				<td>{measurement.timestamp}</td>
			</tr>
			<tr>
				<td>warnings</td>
				<td>{measurement.calibration_warnings.length > 0 ? measurement.calibration_warnings.join(', ') : '-'}</td>
			</tr>
		</tbody>
	</table>

	<table style="margin-bottom: 1.5em">
		<thead>
			<tr>
				<th colspan="5">tof readings</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<th>sensor</th>
				<th>channel</th>
				<th>depth</th>
				<th>delta</th>
				<th>error</th>
			</tr>
			{#each measurement.tof_readings as reading}
				<tr>
					<td>{reading.sensor_index}</td>
					<td>{reading.sensor_channel}</td>
					<td>{formatOptionalNumber(reading.depth_mm)} mm</td>
					<td>{formatOptionalNumber(reading.depth_delta_mm)} mm</td>
					<td>{reading.error ?? '-'}</td>
				</tr>
			{:else}
				<tr>
					<td colspan="5">-</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<div class="action-buttons">
	<button type="button" onclick={didClickMeasure} disabled={isMeasuring === true}>MEASURE</button>
</div>

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if measurementError !== ''}
	<ErrorMessage message={measurementError} />
{/if}

<style>
	.action-buttons {
		display: flex;
		gap: 1em;
		margin-bottom: 1.5em;
	}
</style>
