<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		controlGripper,
		getCalibrationStatus,
		getIOBoxCalibration,
		getIOBoxDoorPosition,
		homeMotor,
		homeToolMotors,
		moveIOBoxDoor,
		moveMotor,
		moveStage,
		moveToIOBoxPosition,
		jogIOBoxAxis,
		testIOBoxSequence,
		testIOBoxMultiItemCycle,
		saveIOBoxCalibration,
		saveIOBoxDoorPosition,
		type ApiErrorDetail,
		type CalibrationStatus,
		type IOBoxDoorPosition,
		type IOBoxPosition,
		type IOBoxPositionName
	} from '$lib/api';
	import { robotStatus } from '$lib/stores';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	type IOBoxAxis = 'x' | 'y' | 'z' | 'stage';
	type IOBoxEditField = Exclude<keyof IOBoxPosition, 'warnings'>;
	type IOBoxEditValues = Record<IOBoxEditField, number>;

	const POSITIONS: IOBoxPositionName[] = ['low', 'high', 'multi_item'];
	const POSITION_LABELS: Record<IOBoxPositionName, string> = {
		low: 'pickup low',
		high: 'pickup high',
		multi_item: 'multi-item'
	};
	// Pickup low and high share one z/stage extension; the multi-item tray pose has its own.
	const POSITION_FIELDS: Record<IOBoxPositionName, Record<IOBoxAxis, IOBoxEditField>> = {
		low: { x: 'pickup_low_x', y: 'pickup_low_y', z: 'z_extension', stage: 'stage_extension' },
		high: { x: 'pickup_high_x', y: 'pickup_high_y', z: 'z_extension', stage: 'stage_extension' },
		multi_item: {
			x: 'multi_item_x',
			y: 'multi_item_y',
			z: 'multi_item_z_extension',
			stage: 'multi_item_stage_extension'
		}
	};
	const EDIT_FIELDS: IOBoxEditField[] = [
		'pickup_low_x',
		'pickup_low_y',
		'pickup_high_x',
		'pickup_high_y',
		'z_extension',
		'stage_extension',
		'multi_item_x',
		'multi_item_y',
		'multi_item_z_extension',
		'multi_item_stage_extension'
	];
	const AXIS_ROWS: IOBoxAxis[][] = [
		['x', 'y'],
		['z', 'stage']
	];

	let ioboxPosition = $state<IOBoxPosition | null>(null);
	let doorPosition = $state<IOBoxDoorPosition | null>(null);
	let loading = $state<boolean>(false);
	let doorLoading = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let warning = $state<string>('');
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);
	let lastStreamNeedsHome = $state<boolean>(false);
	let selectedPosition = $state<IOBoxPositionName>('low');
	let isMoving = $state<boolean>(false);
	let isTesting = $state<boolean>(false);
	let isSaving = $state<boolean>(false);
	let isPositioned = $state<boolean>(false);
	let jogStep = $state<number>(1);

	let editValues = $state<IOBoxEditValues>(
		Object.fromEntries(EDIT_FIELDS.map((field) => [field, 0])) as IOBoxEditValues
	);
	let editDoorClose = $state<number>(0);
	let editDoorOpen = $state<number>(0);

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

	const needsOtherHome = $derived(
		needsHome && (needsHomeAxes.length === 0 || needsHomeAxes.some((axis) => !TOOL_AXES.has(axis)))
	);
	const homeMessage = $derived(
		needsHome ? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}` : ''
	);
	const selectedFields = $derived(POSITION_FIELDS[selectedPosition]);
	const isMultiItem = $derived(selectedPosition === 'multi_item');
	const dirtyAxes = $derived(
		(['x', 'y', 'z', 'stage'] as IOBoxAxis[]).filter(
			(axis) =>
				ioboxPosition !== null &&
				editValues[selectedFields[axis]] !== ioboxPosition[selectedFields[axis]]
		)
	);
	const hasDirtyValues = $derived(dirtyAxes.length > 0);
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
	const editInputsDisabled = $derived(
		isMoving === true || needsHome === true || isPositioned === false
	);
	const doorControlsDisabled = $derived(
		isMoving === true || isTesting === true || isSaving === true || needsHome === true
	);

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadCalibration();
		loadDoorPosition();
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

	async function loadDoorPosition(): Promise<void> {
		doorLoading = true;
		error = '';

		try {
			doorPosition = await getIOBoxDoorPosition();
			editDoorClose = doorPosition.close;
			editDoorOpen = doorPosition.open;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load io door position';
		} finally {
			doorLoading = false;
		}
	}

	async function didClickMoveDoor(position: 'close' | 'open'): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await moveIOBoxDoor(position, editDoorClose, editDoorOpen);
			message = `door moved to ${position} position`;
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : `failed to move door to ${position} position`;
		} finally {
			isMoving = false;
		}
	}

	function didClickResetDoor(): void {
		error = '';
		message = '';
		warning = '';

		if (doorPosition !== null) {
			editDoorClose = doorPosition.close;
			editDoorOpen = doorPosition.open;
			message = 'io door position values reset';
		}
	}

	async function didClickSaveDoor(): Promise<void> {
		isSaving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await saveIOBoxDoorPosition(editDoorClose, editDoorOpen);
			doorPosition = { close: editDoorClose, open: editDoorOpen };
			message = 'io door positions saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save io door positions';
		} finally {
			isSaving = false;
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

	// Re-read /calibration/status when the shared stream reports the crashed
	// motor was homed, so the calibration controls unblock. Only the
	// true->false edge fetches; /calibration/status remains the source of truth.
	// Homing moves the tool off the IO pose, so require [start calibration] again.
	$effect(() => {
		const streamNeedsHome = $robotStatus?.needs_home ?? false;

		if (lastStreamNeedsHome === true && streamNeedsHome === false) {
			isPositioned = false;
			void loadCalibrationStatus();
		}

		lastStreamNeedsHome = streamNeedsHome;
	});

	function didChangePosition(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedPosition = target.value as IOBoxPositionName;
		// Only the selected position's fields are shown, so drop typed values that were never moved.
		syncEditValues();
		isPositioned = false;
		error = '';
		message = '';
		warning = '';
	}

	async function didClickMove(): Promise<void> {
		const previousValues = getEditValues();

		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await moveToIOBoxPosition(selectedPosition);
			syncEditValues();
			restoreEditValues(previousValues, []);
			isPositioned = true;
			message = `started ${POSITION_LABELS[selectedPosition]} calibration`;
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to move to io box position';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: IOBoxAxis, delta: number): Promise<boolean> {
		const previousValues = getEditValues();
		const movedField = selectedFields[axis];

		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			ioboxPosition = await jogIOBoxAxis(selectedPosition, axis, delta);
			syncEditValues();
			restoreEditValues(previousValues, [movedField]);
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
			if (isMultiItem === true) {
				await testIOBoxMultiItemCycle();
				message = 'multi-item tray cycle test completed';
			} else {
				await testIOBoxSequence(true);
				message = 'io box test sequence with gripper completed';
			}
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
		for (const field of EDIT_FIELDS) {
			editValues[field] = ioboxPosition[field];
		}
	}

	function getEditValues(): IOBoxEditValues {
		return { ...editValues };
	}

	function restoreEditValues(values: IOBoxEditValues, movedFields: IOBoxEditField[]): void {
		for (const field of EDIT_FIELDS) {
			if (!movedFields.includes(field)) {
				editValues[field] = values[field];
			}
		}
	}

	async function didClickMoveAxis(axis: IOBoxAxis): Promise<boolean> {
		if (ioboxPosition === null) {
			return false;
		}

		const field = selectedFields[axis];
		const delta = editValues[field] - ioboxPosition[field];

		return didClickJog(axis, delta);
	}

	async function didClickMoveAll(): Promise<void> {
		if (ioboxPosition === null || hasDirtyValues === false || isPositioned === false) {
			return;
		}

		const targetValues = getEditValues();
		const fields = selectedFields;
		const moveWarnings: string[] = [];

		function isTargetDirty(axis: IOBoxAxis): boolean {
			return ioboxPosition !== null && targetValues[fields[axis]] !== ioboxPosition[fields[axis]];
		}

		async function didMoveChangedAxes(axes: IOBoxAxis[]): Promise<boolean> {
			for (const axis of axes) {
				if (isTargetDirty(axis) === false) {
					continue;
				}

				editValues[fields[axis]] = targetValues[fields[axis]];

				const didMove = await didClickMoveAxis(axis);

				if (warning !== '') {
					moveWarnings.push(warning);
				}

				if (didMove === false) {
					return false;
				}
			}

			return true;
		}

		if (isTargetDirty('x') === true || isTargetDirty('y') === true) {
			await didClickMove();

			if (error !== '' || (await didMoveChangedAxes(['x', 'y'])) === false) {
				return;
			}
		}

		if (isTargetDirty('z') === true || isTargetDirty('stage') === true) {
			await didClickExtend();

			if (error !== '' || (await didMoveChangedAxes(['z', 'stage'])) === false) {
				return;
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
			await moveMotor('z_axis', ioboxPosition[selectedFields.z]);
			await moveStage(ioboxPosition[selectedFields.stage]);
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

<div class="scroll-x">
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
						<div class="field-row">
							<span class="field-label">position</span>
							<select
								value={selectedPosition}
								onchange={didChangePosition}
								disabled={isMoving === true || isTesting === true || isSaving === true}
							>
								{#each POSITIONS as position}
									<option value={position}>{POSITION_LABELS[position]}</option>
								{/each}
							</select>
							<button
								class="push-right"
								type="button"
								onclick={didClickMove}
								disabled={isMoving === true ||
									isTesting === true ||
									needsHome === true ||
									isPositioned === true}
							>
								start calibration
							</button>
						</div>
					</td>
				</tr>
				{#each AXIS_ROWS as row}
					<tr>
						{#each row as axis}
							<td>
								<div class="field-row">
									<span class="field-label">{axis}</span>
									<input
										type="number"
										step="0.5"
										bind:value={editValues[selectedFields[axis]]}
										disabled={editInputsDisabled}
									/>
									<button
										class="push-right"
										type="button"
										onclick={() => didClickMoveAxis(axis)}
										disabled={isMoving === true ||
											needsHome === true ||
											isPositioned === false ||
											dirtyAxes.includes(axis) === false}
									>
										move
									</button>
								</div>
							</td>
						{/each}
					</tr>
				{/each}
				<tr>
					<td colspan="2">
						<button
							type="button"
							onclick={didClickMoveAll}
							disabled={isMoving === true ||
								needsHome === true ||
								isPositioned === false ||
								hasDirtyValues === false}
						>
							move all
						</button>
						<button
							type="button"
							onclick={didClickExtend}
							disabled={isMoving === true || needsHome === true || isPositioned === false}
						>
							extend
						</button>
						<button
							type="button"
							onclick={didClickRetract}
							disabled={isMoving === true || needsHome === true || isPositioned === false}
						>
							retract
						</button>
						<button
							type="button"
							onclick={didClickGrip}
							disabled={isMoving === true || needsHome === true}
						>
							grip
						</button>
						<button
							type="button"
							onclick={didClickRelease}
							disabled={isMoving === true || needsHome === true}
						>
							release
						</button>
					</td>
				</tr>
			{/if}
		</tbody>
	</table>
</div>

{#if ioboxPosition !== null}
	{#if isPositioned === true}
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">X</span>
				<button
					type="button"
					onclick={() => didClickJog('x', -jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('x', jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">Y</span>
				<button
					type="button"
					onclick={() => didClickJog('y', -jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('y', jogStep)}
					disabled={isMoving === true || needsHome === true}
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
					disabled={isMoving === true || needsHome === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('z', jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">S</span>
				<button
					type="button"
					onclick={() => didClickJog('stage', -jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					-
				</button>
				<button
					type="button"
					onclick={() => didClickJog('stage', jogStep)}
					disabled={isMoving === true || needsHome === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-step">
				<span class="axis-label">step</span>
				<select bind:value={jogStep} disabled={needsHome === true}>
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
			disabled={isMultiItem === true ||
				isMoving === true ||
				isTesting === true ||
				isSaving === true ||
				needsHome === true ||
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
				hasDirtyValues === true}
		>
			test with gripper
		</button>
		<button
			type="button"
			onclick={didClickReset}
			disabled={isMoving === true || isTesting === true || isSaving === true || needsHome === true}
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
				isPositioned === false ||
				hasDirtyValues === true}
		>
			save
		</button>
	</div>
{/if}

<div class="section-divider" style="margin: 1.5em 0;"></div>

<div class="scroll-x">
	<table>
		<thead>
			<tr>
				<th colspan="2">io door position</th>
			</tr>
		</thead>
		<tbody>
			{#if doorLoading === true}
				<tr>
					<td colspan="2">loading io door position...</td>
				</tr>
			{:else if doorPosition !== null}
				<tr>
					<td>
						<span class="field-label">close</span>
						<input
							type="number"
							min="0"
							step="0.5"
							bind:value={editDoorClose}
							disabled={doorControlsDisabled}
						/>
						<button
							type="button"
							onclick={() => didClickMoveDoor('close')}
							disabled={doorControlsDisabled}
						>
							move
						</button>
					</td>
					<td>
						<span class="field-label">open</span>
						<input
							type="number"
							min="0"
							step="0.5"
							bind:value={editDoorOpen}
							disabled={doorControlsDisabled}
						/>
						<button
							type="button"
							onclick={() => didClickMoveDoor('open')}
							disabled={doorControlsDisabled}
						>
							move
						</button>
					</td>
				</tr>
				<tr>
					<td colspan="2">
						<div class="door-action-buttons">
							<button type="button" onclick={didClickResetDoor} disabled={doorControlsDisabled}>
								reset
							</button>
							<button type="button" onclick={didClickSaveDoor} disabled={doorControlsDisabled}>
								save
							</button>
						</div>
					</td>
				</tr>
			{/if}
		</tbody>
	</table>
</div>

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if warning !== ''}
	<StatusMessage message={`warning: ${warning}`} />
{/if}

{#if error !== '' && duplicateHomeError === false}
	<ErrorMessage message={error} />
{/if}

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

	.field-row {
		display: flex;
		align-items: center;
	}

	.field-row input,
	.field-row select {
		margin-right: 0.5em;
	}

	.push-right {
		margin-left: auto;
	}

	.jog-controls {
		display: flex;
		gap: 1.5em;
		margin-top: 1.5em;
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
		min-width: 1.5em;
		min-height: 1.5em;
		font-size: 1.5em;
		margin-top: 0;
		padding: 2.5px;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1.5em;
	}

	.door-action-buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.section-divider {
		margin-top: 1.5em;
		border-top: 2px black dashed;
	}

	input[type='number'] {
		width: 5em;
	}

	td button {
		margin-top: 0;
	}
</style>
