<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		getCalibrationStatus,
		getIOBoxCalibration,
		homeToolMotors,
		moveToIOBoxPosition,
		jogIOBoxAxis,
		testIOBoxSequence,
		saveIOBoxCalibration,
		type ApiErrorDetail,
		type CalibrationStatus,
		type IOBoxPosition
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let ioboxPosition: IOBoxPosition | null = null;
	let loading = false;
	let error = '';
	let message = '';
	let warning = '';
	let needsHome = false;
	let needsHomeReason = '';
	let needsHomeAxes: string[] = [];
	let isHoming = false;
	let selectedPosition: 'low' | 'high' = 'low';
	let isMoving = false;
	let isTesting = false;
	let isSaving = false;
	let isPositioned = false;
	let jogStep = 1;

	const JOG_STEPS = [
		{ value: 0.1, label: '0.1mm' },
		{ value: 0.25, label: '0.25mm' },
		{ value: 0.5, label: '0.5mm' },
		{ value: 1, label: '1mm' },
		{ value: 2, label: '2mm' },
		{ value: 5, label: '5mm' },
		{ value: 10, label: '1cm' },
		{ value: 100, label: '10cm' }
	];

	const TOOL_AXES = new Set(['z_axis', 'stage']);

	$: needsToolHome = needsHome && needsHomeAxes.some((axis) => TOOL_AXES.has(axis));
	$: needsOtherHome =
		needsHome && (needsHomeAxes.length === 0 || needsHomeAxes.some((axis) => !TOOL_AXES.has(axis)));
	$: homeMessage = needsHome
		? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}`
		: '';

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadCalibration();
		loadCalibrationStatus();
	});

	async function loadCalibration(): Promise<void> {
		loading = true;
		error = '';

		try {
			ioboxPosition = await getIOBoxCalibration();
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to load io box calibration';
		} finally {
			loading = false;
		}
	}

	async function loadCalibrationStatus(): Promise<void> {
		try {
			const status = await getCalibrationStatus();
			applyNeedsHomeStatus(status);
		} catch {
			// Ignore status polling failures; calibration actions will surface errors.
		}
	}

	function applyNeedsHomeStatus(status: CalibrationStatus): void {
		needsHome = status.needs_home;
		needsHomeReason = status.needs_home_reason ?? '';
		needsHomeAxes = status.needs_home_axes ?? [];
	}

	function applyNeedsHomeDetail(detail?: ApiErrorDetail): void {
		if (detail?.needs_home !== true) {
			return;
		}
		needsHome = true;
		needsHomeReason = detail.reason ?? detail.message ?? needsHomeReason;
		needsHomeAxes = detail.needs_home_axes ?? [];
	}

	function handleNeedsHomeError(err: unknown): void {
		if (err instanceof ApiError) {
			applyNeedsHomeDetail(err.detail);
		}
	}

	async function didClickHomeToolMotors(): Promise<void> {
		isHoming = true;
		error = '';
		message = '';
		warning = '';

		try {
			const status = await homeToolMotors();
			applyNeedsHomeStatus(status);
			message = 'tool motors homed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to home tool motors';
		} finally {
			isHoming = false;
		}
	}

	function didChangePosition(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedPosition = target.value as 'low' | 'high';
		isPositioned = false;
		error = '';
		message = '';
		warning = '';
	}

	async function didClickMove(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await moveToIOBoxPosition(selectedPosition);
			isPositioned = true;
			message = `moved to ${selectedPosition} position`;
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to move to io box position';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await jogIOBoxAxis(selectedPosition, axis, delta);
			const warnings = ioboxPosition?.warnings ?? [];
			warning = warnings.length > 0 ? warnings.join('; ') : '';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to jog io box axis';
		} finally {
			isMoving = false;
		}
	}

	async function didClickTest(): Promise<void> {
		isTesting = true;
		error = '';
		message = '';
		warning = '';

		try {
			await testIOBoxSequence(false);
			message = 'io box test sequence completed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to run io box test sequence';
		} finally {
			isTesting = false;
		}
	}

	async function didClickTestWithGripper(): Promise<void> {
		isTesting = true;
		error = '';
		message = '';
		warning = '';

		try {
			await testIOBoxSequence(true);
			message = 'io box test sequence with gripper completed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to run io box test sequence with gripper';
		} finally {
			isTesting = false;
		}
	}

	async function didClickSave(): Promise<void> {
		if (ioboxPosition === null) {
			error = 'no io box position to save';
			return;
		}

		isSaving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await saveIOBoxCalibration(ioboxPosition);
			await loadCalibrationStatus();
			message = 'io box calibration saved and robot homed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to save io box calibration';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await getIOBoxCalibration();
			message = 'io box position reset from config';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to reset io box position';
		} finally {
			isMoving = false;
		}
	}

	function formatNumber(value: number): string {
		return value.toFixed(2);
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="4">io box calibration</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="4">loading io box calibration...</td>
			</tr>
		{:else if ioboxPosition !== null}
			<tr>
				<td colspan="2">
					<span class="field-label">position</span>
					<select value={selectedPosition} on:change={didChangePosition}>
						<option value="low">pickup low</option>
						<option value="high">pickup high</option>
					</select>
				</td>
				<td colspan="2">
					<button
						class="move-button"
						type="button"
						on:click={didClickMove}
						disabled={isMoving === true || isTesting === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td>low x: {formatNumber(ioboxPosition.pickup_low_x)}</td>
				<td>low y: {formatNumber(ioboxPosition.pickup_low_y)}</td>
				<td>high x: {formatNumber(ioboxPosition.pickup_high_x)}</td>
				<td>high y: {formatNumber(ioboxPosition.pickup_high_y)}</td>
			</tr>
			<tr>
				<td colspan="2">Z: {formatNumber(ioboxPosition.z_extension)}</td>
				<td colspan="2">stage: {formatNumber(ioboxPosition.stage_extension)}</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if homeMessage !== ''}
	<ErrorMessage message={homeMessage} />
{/if}

{#if needsToolHome === true}
	<div class="action-buttons">
		<button
			type="button"
			on:click={didClickHomeToolMotors}
			disabled={isHoming === true || isMoving === true || isTesting === true || isSaving === true}
		>
			{isHoming === true ? 'homing...' : 'home tool motors'}
		</button>
	</div>
{/if}

{#if needsOtherHome === true}
	<StatusMessage message="other motors require homing; use the motors page" />
{/if}

{#if ioboxPosition !== null}
	{#if isPositioned === true}
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">X</span>
				<button
					type="button"
					on:click={() => didClickJog('x', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('x', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">Y</span>
				<button
					type="button"
					on:click={() => didClickJog('y', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('y', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">Z</span>
				<button
					type="button"
					on:click={() => didClickJog('z', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('z', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">S</span>
				<button
					type="button"
					on:click={() => didClickJog('stage', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('stage', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-step">
				<span class="axis-label">step</span>
				<select bind:value={jogStep} disabled={needsHome === true || isHoming === true}>
					{#each JOG_STEPS as step}
						<option value={step.value}>{step.label}</option>
					{/each}
				</select>
			</div>
		</div>
	{/if}

	<div class="action-buttons">
	<button
		type="button"
		on:click={didClickTest}
		disabled={isMoving === true || isTesting === true || isSaving === true || needsHome === true || isHoming === true}
	>
			test
		</button>
	<button
		type="button"
		on:click={didClickTestWithGripper}
		disabled={isMoving === true || isTesting === true || isSaving === true || needsHome === true || isHoming === true}
	>
			test gripper
		</button>
	<button
		type="button"
		on:click={didClickReset}
		disabled={isMoving === true || isTesting === true || isSaving === true || needsHome === true || isHoming === true}
	>
			reset
		</button>
	<button
		type="button"
		on:click={didClickSave}
		disabled={isMoving === true || isTesting === true || isSaving === true || needsHome === true || isHoming === true}
	>
			save
		</button>
	</div>
{/if}

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if warning !== ''}
	<StatusMessage message={`warning: ${warning}`} />
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.field-label {
		margin-right: 0.5em;
	}

	.jog-controls {
		display: flex;
		gap: 2em;
		margin-top: 2em;
		margin-bottom: 1em;
	}

	.jog-axis,
	.jog-step {
		display: flex;
		align-items: center;
		gap: 0.5em;
	}

	.axis-label {
		font-weight: bold;
	}

	.jog-axis button {
		min-width: 2em;
		min-height: 2em;
		font-size: 1.5em;
		margin-top: 0;
		padding: 2.5px;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
	}

	.move-button {
		margin-top: 0;
	}
</style>
