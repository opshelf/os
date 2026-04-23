<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		controlGripper,
		getCalibrationStatus,
		getIOBoxCalibration,
		homeMotor,
		homeToolMotors,
		moveMotor,
		moveStage,
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

	let ioboxPosition = $state<IOBoxPosition | null>(null);
	let loading = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let warning = $state<string>('');
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);
	let isHoming = $state<boolean>(false);
	let selectedPosition = $state<'low' | 'high'>('low');
	let isMoving = $state<boolean>(false);
	let isTesting = $state<boolean>(false);
	let isSaving = $state<boolean>(false);
	let isPositioned = $state<boolean>(false);
	let jogStep = $state<number>(1);

	let editLowX = $state<number>(0);
	let editLowY = $state<number>(0);
	let editHighX = $state<number>(0);
	let editHighY = $state<number>(0);
	let editZ = $state<number>(0);
	let editStage = $state<number>(0);

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

	const needsToolHome = $derived(needsHome && needsHomeAxes.some((axis) => TOOL_AXES.has(axis)));
	const needsOtherHome = $derived(
		needsHome && (needsHomeAxes.length === 0 || needsHomeAxes.some((axis) => !TOOL_AXES.has(axis)))
	);
	const homeMessage = $derived(
		needsHome ? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}` : ''
	);
	const duplicateHomeError = $derived.by(() => {
		if (needsHome !== true || error === '') {
			return false;
		}

		const errorLower = error.toLowerCase();
		const reasonLower = needsHomeReason.toLowerCase();

		if (needsHomeReason !== '' && errorLower.includes(reasonLower)) {
			return true;
		}

		return needsHomeReason === '' && errorLower.includes('home required');
	});
	const homingDisabled = $derived(
		isHoming === true ||
			isMoving === true ||
			isTesting === true ||
			isSaving === true ||
			!needsToolHome
	);

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
			syncEditValues();
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
			syncEditValues();
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
			syncEditValues();
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
			error =
				err instanceof Error ? err.message : 'failed to run io box test sequence with gripper';
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
		let resetComplete = false;

		try {
			ioboxPosition = await getIOBoxCalibration();
			syncEditValues();
			resetComplete = true;
			const status = await homeToolMotors();
			applyNeedsHomeStatus(status);
			message = 'io box position reset from config and tool motors homed';
		} catch (err) {
			handleNeedsHomeError(err);
			if (resetComplete) {
				message = 'io box position reset from config';
				error = err instanceof Error ? err.message : 'failed to home tool motors after reset';
			} else {
				error = err instanceof Error ? err.message : 'failed to reset io box position';
			}
		} finally {
			isMoving = false;
		}
	}

	function formatNumber(value: number): string {
		return value.toFixed(2);
	}

	function syncEditValues(): void {
		if (ioboxPosition === null) {
			return;
		}
		editLowX = ioboxPosition.pickup_low_x;
		editLowY = ioboxPosition.pickup_low_y;
		editHighX = ioboxPosition.pickup_high_x;
		editHighY = ioboxPosition.pickup_high_y;
		editZ = ioboxPosition.z_extension;
		editStage = ioboxPosition.stage_extension;
	}

	async function didClickMoveLowX(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editLowX - ioboxPosition.pickup_low_x;

		selectedPosition = 'low';
		await didClickJog('x', -delta);
	}

	async function didClickMoveLowY(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editLowY - ioboxPosition.pickup_low_y;

		selectedPosition = 'low';
		await didClickJog('y', delta);
	}

	async function didClickMoveHighX(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editHighX - ioboxPosition.pickup_high_x;

		selectedPosition = 'high';
		await didClickJog('x', -delta);
	}

	async function didClickMoveHighY(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editHighY - ioboxPosition.pickup_high_y;

		selectedPosition = 'high';
		await didClickJog('y', delta);
	}

	async function didClickMoveZ(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editZ - ioboxPosition.z_extension;

		await didClickJog('z', delta);
	}

	async function didClickMoveStage(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		const delta = editStage - ioboxPosition.stage_extension;

		await didClickJog('stage', delta);
	}

	async function didClickExtend(): Promise<void> {
		if (ioboxPosition === null) {
			return;
		}

		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await moveMotor('z_axis', ioboxPosition.z_extension);
			await moveStage(ioboxPosition.stage_extension);
			message = 'stage and z extended';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to extend';
		} finally {
			isMoving = false;
		}
	}

	async function didClickRetract(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await homeMotor('stage');
			await homeMotor('z_axis');
			await loadCalibration();
			message = 'stage and z retracted';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to retract';
		} finally {
			isMoving = false;
		}
	}

	async function didClickGrip(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await controlGripper('grip');
			message = 'gripper gripped';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to grip';
		} finally {
			isMoving = false;
		}
	}

	async function didClickRelease(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await controlGripper('release');
			message = 'gripper released';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to release gripper';
		} finally {
			isMoving = false;
		}
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="2">io box calibration</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="2">loading io box calibration...</td>
			</tr>
		{:else if ioboxPosition !== null}
			<tr>
				<td colspan="2">
					<span class="field-label">position</span>
					<select value={selectedPosition} onchange={didChangePosition}>
						<option value="low">pickup low</option>
						<option value="high">pickup high</option>
					</select>
					<button
						class="move-button"
						type="button"
						onclick={didClickMove}
						disabled={isMoving === true ||
							isTesting === true ||
							needsHome === true ||
							isHoming === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td>
					<span class="field-label">low x</span>
					<input
						type="number"
						step="0.5"
						bind:value={editLowX}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveLowX}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
				<td>
					<span class="field-label">low y</span>
					<input
						type="number"
						step="0.5"
						bind:value={editLowY}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveLowY}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td>
					<span class="field-label">high x</span>
					<input
						type="number"
						step="0.5"
						bind:value={editHighX}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveHighX}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
				<td>
					<span class="field-label">high y</span>
					<input
						type="number"
						step="0.5"
						bind:value={editHighY}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveHighY}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td>
					<span class="field-label">z</span>
					<input
						type="number"
						step="0.5"
						bind:value={editZ}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveZ}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
				<td>
					<span class="field-label">stage</span>
					<input
						type="number"
						step="0.5"
						bind:value={editStage}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					/>
					<button
						type="button"
						onclick={didClickMoveStage}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td colspan="2">
					<button
						type="button"
						onclick={didClickExtend}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false}
					>
						extend
					</button>
					<button
						type="button"
						onclick={didClickRetract}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false}
					>
						retract
					</button>
					<button
						type="button"
						onclick={didClickGrip}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						&#x2194;&#xFE0E;
					</button>
					<button
						type="button"
						onclick={didClickRelease}
						disabled={isMoving === true || needsHome === true || isHoming === true}
					>
						&#x2192;&#x2190;
					</button>
				</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if ioboxPosition !== null}
	{#if isPositioned === true}
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">X</span>
				<button
					type="button"
					onclick={() => didClickJog('x', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('x', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">Y</span>
				<button
					type="button"
					onclick={() => didClickJog('y', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('y', jogStep)}
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
					onclick={() => didClickJog('z', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('z', jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">S</span>
				<button
					type="button"
					onclick={() => didClickJog('stage', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('stage', jogStep)}
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
			onclick={didClickTest}
			disabled={isMoving === true ||
				isTesting === true ||
				isSaving === true ||
				needsHome === true ||
				isHoming === true}
		>
			test
		</button>
		<button
			type="button"
			onclick={didClickTestWithGripper}
			disabled={isMoving === true ||
				isTesting === true ||
				isSaving === true ||
				needsHome === true ||
				isHoming === true}
		>
			test with gripper
		</button>
		<button
			type="button"
			onclick={didClickReset}
			disabled={isMoving === true ||
				isTesting === true ||
				isSaving === true ||
				needsHome === true ||
				isHoming === true}
		>
			reset
		</button>
		<button
			type="button"
			onclick={didClickSave}
			disabled={isMoving === true ||
				isTesting === true ||
				isSaving === true ||
				needsHome === true ||
				isHoming === true}
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

{#if error !== '' && duplicateHomeError === false}
	<ErrorMessage message={error} />
{/if}

<div class="section-divider"></div>

<div class="action-buttons home-actions">
	<button type="button" onclick={didClickHomeToolMotors} disabled={homingDisabled}>
		{isHoming === true ? 'homing...' : 'home tool motors'}
	</button>
</div>

{#if homeMessage !== ''}
	<ErrorMessage message={homeMessage} />
{/if}

{#if needsOtherHome === true}
	<StatusMessage message="other motors require homing; use the motors page" />
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
		margin-top: 1.5em;
	}

	.section-divider {
		margin-top: 2em;
		border-top: 2px black dashed;
	}

	.home-actions {
		margin-top: 2em;
		align-items: center;
	}

	.home-actions button {
		margin-top: 0;
	}

	.move-button {
		margin-top: 0;
	}

	input[type='number'] {
		width: 5em;
	}

	td button {
		margin-top: 0;
	}
</style>
