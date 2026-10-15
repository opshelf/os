<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		calibrateSizerTofBlank,
		captureSizerCameraBlank,
		getSizerConfig,
		setSizerConfig,
		type SizerConfig
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let config: SizerConfig | null = $state(null);

	let configError = $state('');
	let message = $state('');

	let isLoading = $state(true);
	let isRefreshing = $state(false);
	let isApplying = $state(false);
	let isCapturingBlank = $state(false);
	let isCalibratingTof = $state(false);

	let mmPerPxX = $state('');
	let mmPerPxY = $state('');

	function setCalibrationInputs(nextConfig: SizerConfig): void {
		config = nextConfig;
		mmPerPxX = formatOptionalInput(nextConfig.calibration.mm_per_px_x);
		mmPerPxY = formatOptionalInput(nextConfig.calibration.mm_per_px_y);
	}

	async function fetchConfig(): Promise<void> {
		try {
			setCalibrationInputs(await getSizerConfig());
			configError = '';
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to read calibration parameters';
		}
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		configError = '';

		try {
			await fetchConfig();
		} finally {
			isRefreshing = false;
		}
	}

	async function didClickApplyParams(): Promise<void> {
		const nextConfig = buildConfig();
		if (nextConfig === null) {
			return;
		}

		isApplying = true;
		configError = '';
		message = '';

		try {
			const result = await setSizerConfig(nextConfig);
			if (result.success === true) {
				setCalibrationInputs(result.config);
				message = 'calibration parameters applied';
			} else {
				configError = 'failed to apply calibration parameters';
			}
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to apply calibration parameters';
		} finally {
			isApplying = false;
		}
	}

	async function didClickCalibrateTofBlank(): Promise<void> {
		isCalibratingTof = true;
		configError = '';
		message = '';

		try {
			await calibrateSizerTofBlank();
			await fetchConfig();
			message = 'tof blank calibrated';
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to calibrate tof blank';
		} finally {
			isCalibratingTof = false;
		}
	}

	async function didClickCaptureCameraBlank(): Promise<void> {
		isCapturingBlank = true;
		configError = '';
		message = '';

		try {
			await captureSizerCameraBlank();
			await fetchConfig();
			message = 'camera blank captured';
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to capture camera blank';
		} finally {
			isCapturingBlank = false;
		}
	}

	function buildConfig(): SizerConfig | null {
		if (config === null) {
			configError = 'sizer config not loaded';
			return null;
		}

		return {
			...config,
			calibration: {
				...config.calibration,
				mm_per_px_x: parseOptionalNumber(mmPerPxX),
				mm_per_px_y: parseOptionalNumber(mmPerPxY)
			}
		};
	}

	function parseOptionalNumber(value: string | number | null | undefined): number | null {
		if (value === null || value === undefined) {
			return null;
		}

		const trimmed = String(value).trim();
		if (trimmed === '') {
			return null;
		}

		const parsed = Number(trimmed);
		return Number.isFinite(parsed) ? parsed : null;
	}

	function formatOptionalInput(value: number | null): string {
		return value === null ? '' : String(value);
	}

	function isActionBusy(): boolean {
		return isApplying || isCalibratingTof || isCapturingBlank;
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchConfig();
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
	<div class="scroll-x" style="margin-bottom: 1.5em">
		<table>
			<thead>
				<tr>
					<th colspan="2">dimension calibration</th>
				</tr>
			</thead>
			<tbody>
				<tr
					><td>mm per px x</td><td
						><input
							type="number"
							bind:value={mmPerPxX}
							min="0"
							step="0.0001"
							placeholder="none"
						/></td
					></tr
				>
				<tr
					><td>mm per px y</td><td
						><input
							type="number"
							bind:value={mmPerPxY}
							min="0"
							step="0.0001"
							placeholder="none"
						/></td
					></tr
				>
			</tbody>
		</table>
	</div>

	<div class="action-buttons">
		<button type="button" onclick={didClickCaptureCameraBlank} disabled={isCapturingBlank === true}
			>capture camera blank</button
		>
		<button type="button" onclick={didClickCalibrateTofBlank} disabled={isCalibratingTof === true}
			>calibrate tof blank</button
		>
	</div>

	<div class="action-buttons">
		<button
			type="button"
			onclick={didClickRefresh}
			disabled={isRefreshing === true || isActionBusy() === true}>refresh</button
		>
		<button type="button" onclick={didClickApplyParams} disabled={isApplying === true}
			>save params</button
		>
	</div>

	{#if configError !== ''}
		<ErrorMessage message={configError} />
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}
{/if}

<style>
	input {
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	input[type='number'] {
		width: 8em;
	}

	.action-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 1em;
		margin-bottom: 1.5em;
	}
</style>
