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

	type IOBoxEditField = 'lowX' | 'lowY' | 'highX' | 'highY' | 'z' | 'stage';

	interface IOBoxEditValues {
		lowX: number;
		lowY: number;
		highX: number;
		highY: number;
		z: number;
		stage: number;
	}

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
	const isLowXDirty = $derived(ioboxPosition !== null && editLowX !== ioboxPosition.pickup_low_x);
	const isLowYDirty = $derived(ioboxPosition !== null && editLowY !== ioboxPosition.pickup_low_y);
	const isHighXDirty = $derived(ioboxPosition !== null && editHighX !== ioboxPosition.pickup_high_x);
	const isHighYDirty = $derived(ioboxPosition !== null && editHighY !== ioboxPosition.pickup_high_y);
	const isZDirty = $derived(ioboxPosition !== null && editZ !== ioboxPosition.z_extension);
	const isStageDirty = $derived(ioboxPosition !== null && editStage !== ioboxPosition.stage_extension);
	const hasDirtyValues = $derived(
		isLowXDirty === true ||
			isLowYDirty === true ||
			isHighXDirty === true ||
			isHighYDirty === true ||
			isZDirty === true ||
			isStageDirty === true
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
	const editInputsDisabled = $derived(
		isMoving === true || needsHome === true || isHoming === true || isPositioned === false
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
			isPositioned = false;
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
		const editValues = getEditValues();

		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await moveToIOBoxPosition(selectedPosition);
			syncEditValues();
			restoreEditValues(editValues, []);
			isPositioned = true;
			message = `started ${selectedPosition} calibration`;
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to move to io box position';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<boolean> {
		const editValues = getEditValues();
		const movedField = getMovedField(selectedPosition, axis);

		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await jogIOBoxAxis(selectedPosition, axis, delta);
			syncEditValues();
			restoreEditValues(editValues, [movedField]);
			const warnings = ioboxPosition?.warnings ?? [];
			warning = warnings.length > 0 ? warnings.join('; ') : '';
			return true;
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to jog io box axis';
			return false;
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
			isPositioned = false;
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
			isPositioned = false;
			message = 'io box position reset from config and tool motors homed';
		} catch (err) {
			handleNeedsHomeError(err);
			if (resetComplete) {
				message = 'io box position reset from config';
				isPositioned = false;
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

	function getEditValues(): IOBoxEditValues {
		return {
			lowX: editLowX,
			lowY: editLowY,
			highX: editHighX,
			highY: editHighY,
			z: editZ,
			stage: editStage
		};
	}

	function restoreEditValues(values: IOBoxEditValues, movedFields: IOBoxEditField[]): void {
		if (!movedFields.includes('lowX')) {
			editLowX = values.lowX;
		}
		if (!movedFields.includes('lowY')) {
			editLowY = values.lowY;
		}
		if (!movedFields.includes('highX')) {
			editHighX = values.highX;
		}
		if (!movedFields.includes('highY')) {
			editHighY = values.highY;
		}
		if (!movedFields.includes('z')) {
			editZ = values.z;
		}
		if (!movedFields.includes('stage')) {
			editStage = values.stage;
		}
	}

	function getMovedField(position: 'low' | 'high', axis: 'x' | 'y' | 'z' | 'stage'): IOBoxEditField {
		if (axis === 'z' || axis === 'stage') {
			return axis;
		}

		if (position === 'low') {
			return axis === 'x' ? 'lowX' : 'lowY';
		}

		return axis === 'x' ? 'highX' : 'highY';
	}

	async function didClickMoveLowX(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editLowX - ioboxPosition.pickup_low_x;

		selectedPosition = 'low';
		return didClickJog('x', delta);
	}

	async function didClickMoveLowY(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editLowY - ioboxPosition.pickup_low_y;

		selectedPosition = 'low';
		return didClickJog('y', delta);
	}

	async function didClickMoveHighX(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editHighX - ioboxPosition.pickup_high_x;

		selectedPosition = 'high';
		return didClickJog('x', delta);
	}

	async function didClickMoveHighY(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editHighY - ioboxPosition.pickup_high_y;

		selectedPosition = 'high';
		return didClickJog('y', delta);
	}

	async function didClickMoveZ(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editZ - ioboxPosition.z_extension;

		return didClickJog('z', delta);
	}

	async function didClickMoveStage(): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const delta = editStage - ioboxPosition.stage_extension;

		return didClickJog('stage', delta);
	}

	async function didClickMoveAll(): Promise<void> {
		if (ioboxPosition === null || hasDirtyValues === false || isPositioned === false) {
			return;
		}

		const targetValues = getEditValues();
		const moveWarnings: string[] = [];

		async function didMoveChangedField(move: () => Promise<boolean>): Promise<boolean> {
			const didMove = await move();

			if (warning !== '') {
				moveWarnings.push(warning);
			}

			return didMove;
		}

		async function didStartPosition(position: 'low' | 'high'): Promise<boolean> {
			selectedPosition = position;
			await didClickMove();

			return error === '';
		}

		if (targetValues.lowX !== ioboxPosition.pickup_low_x || targetValues.lowY !== ioboxPosition.pickup_low_y) {
			if ((await didStartPosition('low')) === false) {
				return;
			}

			if (targetValues.lowX !== ioboxPosition.pickup_low_x) {
				editLowX = targetValues.lowX;
				if ((await didMoveChangedField(didClickMoveLowX)) === false) {
					return;
				}
			}

			if (targetValues.lowY !== ioboxPosition.pickup_low_y) {
				editLowY = targetValues.lowY;
				if ((await didMoveChangedField(didClickMoveLowY)) === false) {
					return;
				}
			}
		}

		if (targetValues.highX !== ioboxPosition.pickup_high_x || targetValues.highY !== ioboxPosition.pickup_high_y) {
			if ((await didStartPosition('high')) === false) {
				return;
			}

			if (targetValues.highX !== ioboxPosition.pickup_high_x) {
				editHighX = targetValues.highX;
				if ((await didMoveChangedField(didClickMoveHighX)) === false) {
					return;
				}
			}

			if (targetValues.highY !== ioboxPosition.pickup_high_y) {
				editHighY = targetValues.highY;
				if ((await didMoveChangedField(didClickMoveHighY)) === false) {
					return;
				}
			}
		}

		if (targetValues.z !== ioboxPosition.z_extension || targetValues.stage !== ioboxPosition.stage_extension) {
			await didClickExtend();

			if (error !== '') {
				return;
			}

			if (targetValues.z !== ioboxPosition.z_extension) {
				editZ = targetValues.z;
				if ((await didMoveChangedField(didClickMoveZ)) === false) {
					return;
				}
			}

			if (targetValues.stage !== ioboxPosition.stage_extension) {
				editStage = targetValues.stage;
				if ((await didMoveChangedField(didClickMoveStage)) === false) {
					return;
				}
			}
		}

		warning = moveWarnings.join('; ');
		message = 'moved all changed values';
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
			isPositioned = false;
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
							isHoming === true ||
							isPositioned === true}
					>
						start calibration
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveLowX}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isLowXDirty === false}
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveLowY}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isLowYDirty === false}
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveHighX}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isHighXDirty === false}
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveHighY}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isHighYDirty === false}
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveZ}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isZDirty === false}
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
						disabled={editInputsDisabled}
					/>
					<button
						type="button"
						onclick={didClickMoveStage}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							isStageDirty === false}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td colspan="2">
					<button
						type="button"
						onclick={didClickMoveAll}
						disabled={isMoving === true ||
							needsHome === true ||
							isHoming === true ||
							isPositioned === false ||
							hasDirtyValues === false}
					>
						move all to changed values
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
					onclick={() => didClickJog('x', -jogStep)}
					disabled={isMoving === true || needsHome === true || isHoming === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('x', jogStep)}
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
				isHoming === true ||
				hasDirtyValues === true}
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
				isHoming === true ||
				hasDirtyValues === true}
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
				isHoming === true ||
				isPositioned === false ||
				hasDirtyValues === true}
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
