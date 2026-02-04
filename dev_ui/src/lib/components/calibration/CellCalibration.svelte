<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		getStorageGrid,
		getCalibrationStatus,
		homeToolMotors,
		moveToCell,
		jogAxis,
		testGripper,
		saveCalibration,
		getRotationAngles,
		rotateToAngle,
		moveStage,
		homeMotor,
		getShelfStageExtension,
		controlGripper,
		type ApiErrorDetail,
		type CalibrationStatus,
		type StorageGrid,
		type Position
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';
	import ShelfCalibration from '$lib/components/calibration/ShelfCalibration.svelte';

	let storageGrid = $state<StorageGrid | null>(null);
	let loading = $state<boolean>(true);
	let loadError = $state<string>('');

	let selectedSide = $state<'left_module' | 'right_module'>('left_module');
	let selectedShelf = $state<number>(0);
	let selectedCell = $state<number>(0);
	let currentPosition = $state<Position | null>(null);
	let isMoving = $state<boolean>(false);
	let isTesting = $state<boolean>(false);
	let isSaving = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let warning = $state<string>('');
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);
	let isHoming = $state<boolean>(false);

	const TOOL_AXES = new Set(['z_axis', 'stage']);

	let jogStep = $state<number>(1);

	let editX = $state<number>(0);
	let editY = $state<number>(0);
	let editZ = $state<number>(0);
	let isExtending = $state<boolean>(false);
	let stageExtensionValue = $state<number>(0);

	const JOG_STEPS = [
		{ value: 0.1, label: '0.1mm' },
		{ value: 0.25, label: '0.25mm' },
		{ value: 0.5, label: '0.5mm' },
		{ value: 1, label: '1mm' },
		{ value: 2, label: '2mm' },
		{ value: 5, label: '5mm' },
		{ value: 10, label: '1cm' },
		{ value: 20, label: '2cm' },
		{ value: 50, label: '5cm' },
		{ value: 100, label: '10cm' }
	];

	const needsToolHome = $derived(needsHome && needsHomeAxes.some((axis) => TOOL_AXES.has(axis)));
	const needsOtherHome = $derived(
		needsHome &&
			(needsHomeAxes.length === 0 || needsHomeAxes.some((axis) => !TOOL_AXES.has(axis)))
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
		isHoming === true || isMoving === true || isTesting === true || isSaving === true || !needsToolHome
	);

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadStorage();
		loadCalibrationStatus();
	});

	async function loadStorage(): Promise<void> {
		loading = true;
		loadError = '';

		try {
			storageGrid = await getStorageGrid();
			selectedShelf = 0;
			selectedCell = 0;
		} catch (err) {
			handleNeedsHomeError(err);
			loadError = err instanceof Error ? err.message : 'failed to load storage grid';
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

	function getShelves() {
		if (storageGrid === null) {
			return [];
		}

		if (selectedSide === 'left_module') {
			return storageGrid.left_module;
		}

		return storageGrid.right_module;
	}

	function getCells() {
		const shelves = getShelves();

		if (selectedShelf >= shelves.length) {
			return [];
		}

		return shelves[selectedShelf].cells;
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'left_module' | 'right_module';
		selectedShelf = 0;
		selectedCell = 0;
		currentPosition = null;
		error = '';
		message = '';
		warning = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		selectedCell = 0;
		currentPosition = null;
		error = '';
		message = '';
		warning = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = parseInt(target.value, 10);
		currentPosition = null;
		error = '';
		message = '';
		warning = '';
	}

	async function didClickCalibrate(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			storageGrid = await getStorageGrid();
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);

			const rotationAngles = await getRotationAngles();
			const targetAngle =
				selectedSide === 'right_module' ? rotationAngles.back : rotationAngles.front;

			await rotateToAngle(targetAngle);
			await loadStageExtensionForSide();

			syncEditValues();
			message = 'moved to cell';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to move to cell';
		} finally {
			isMoving = false;
		}
	}

	async function loadStageExtensionForSide(): Promise<void> {
		try {
			const ext = await getShelfStageExtension();

			stageExtensionValue =
				selectedSide === 'left_module' ? ext.front : ext.back;
		} catch {
			stageExtensionValue = 0;
		}
	}

	async function didClickExtend(): Promise<void> {
		if (stageExtensionValue === 0) {
			return;
		}

		isExtending = true;
		error = '';
		message = '';
		warning = '';

		try {
			const result = await moveStage(stageExtensionValue);

			message = 'stage extended';
			warning =
				result.warnings !== undefined && result.warnings.length > 0
					? result.warnings.join('; ')
					: '';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to extend stage';
		} finally {
			isExtending = false;
		}
	}

	async function didClickRetract(): Promise<void> {
		isExtending = true;
		error = '';
		message = '';
		warning = '';

		try {
			await homeMotor('stage');
			message = 'stage retracted';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to retract stage';
		} finally {
			isExtending = false;
		}
	}

	function syncEditValues(): void {
		if (currentPosition === null) {
			return;
		}
		editX = currentPosition.x;
		editY = currentPosition.y;
		editZ = currentPosition.z_offset;
	}

	async function didClickMoveX(): Promise<void> {
		if (currentPosition === null) {
			return;
		}

		const delta = editX - currentPosition.x;

		if (delta === 0) {
			return;
		}

		await didClickJog('x', delta);
	}

	async function didClickMoveY(): Promise<void> {
		if (currentPosition === null) {
			return;
		}

		const delta = editY - currentPosition.y;

		if (delta === 0) {
			return;
		}

		await didClickJog('y', delta);
	}

	async function didClickMoveZ(): Promise<void> {
		if (currentPosition === null) {
			return;
		}

		const delta = editZ - currentPosition.z_offset;

		if (delta === 0) {
			return;
		}

		await didClickJog('z', delta);
	}

	async function didClickJog(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			currentPosition = await jogAxis(axis, delta);
			syncEditValues();
			const warnings = currentPosition?.warnings ?? [];
			warning = warnings.length > 0 ? warnings.join('; ') : '';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to jog axis';
		} finally {
			isMoving = false;
		}
	}

	async function didClickSaveAndTest(): Promise<void> {
		if (currentPosition === null) {
			error = 'no position to save';
			return;
		}

		isTesting = true;
		error = '';
		message = '';
		warning = '';

		try {
			await saveCalibration(
				selectedSide,
				selectedShelf,
				selectedCell,
				currentPosition.x,
				currentPosition.y,
				currentPosition.z_offset
			);

			storageGrid = await getStorageGrid();

			await testGripper(selectedSide, selectedShelf, selectedCell);
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);
			syncEditValues();
			message = 'saved and test completed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to save and test';
		} finally {
			isTesting = false;
		}
	}

	async function didClickGripperOpen(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await controlGripper('release');
			message = 'gripper opened';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to open gripper';
		} finally {
			isMoving = false;
		}
	}

	async function didClickGripperClose(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await controlGripper('grip');
			message = 'gripper closed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to close gripper';
		} finally {
			isMoving = false;
		}
	}

	async function didClickSave(): Promise<void> {
		if (currentPosition === null) {
			error = 'no position to save';
			return;
		}

		isSaving = true;
		error = '';
		message = '';
		warning = '';

		try {
			await saveCalibration(
				selectedSide,
				selectedShelf,
				selectedCell,
				currentPosition.x,
				currentPosition.y,
				currentPosition.z_offset
			);

			storageGrid = await getStorageGrid();

			message = 'calibration saved';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to save calibration';
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
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);
			syncEditValues();
			message = 'position reset';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to reset position';
		} finally {
			isMoving = false;
		}
	}

	function handleShelfNeedsHome(event: CustomEvent<CalibrationStatus>): void {
		applyNeedsHomeStatus(event.detail);
	}

	function formatNumber(value: number): string {
		return value.toFixed(2);
	}
</script>

{#if loading === true}
	<p>loading storage grid...</p>
{:else if loadError !== ''}
	<ErrorMessage message={loadError} />
{:else if storageGrid !== null}
	<table>
		<thead>
			<tr>
				<th colspan="3">cell selection</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>
					<span class="field-label">side</span>
					<select value={selectedSide} onchange={didChangeSide}>
						<option value="left_module">left module</option>
						<option value="right_module">right module</option>
					</select>
				</td>
				<td>
					<span class="field-label">shelf</span>
					<select value={selectedShelf} onchange={didChangeShelf}>
						{#each getShelves() as _, index}
							<option value={index}>{index}</option>
						{/each}
					</select>
				</td>
				<td>
					<span class="field-label">cell</span>
					<select value={selectedCell} onchange={didChangeCell}>
						{#each getCells() as _, index}
							<option value={index}>{index}</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="calibrate-row">
						<button
							class="calibrate-button"
							type="button"
							onclick={didClickCalibrate}
							disabled={isMoving === true ||
								isTesting === true ||
								isExtending === true ||
								needsHome === true ||
								isHoming === true}
						>
							calibrate
						</button>
						{#if currentPosition !== null}
							<button
								type="button"
								onclick={didClickExtend}
								disabled={isMoving === true ||
									isTesting === true ||
									isExtending === true ||
									needsHome === true ||
									isHoming === true ||
									stageExtensionValue === 0}
							>
								extend
							</button>
							<button
								type="button"
								onclick={didClickRetract}
								disabled={isMoving === true ||
									isTesting === true ||
									isExtending === true ||
									needsHome === true ||
									isHoming === true}
							>
								retract
							</button>
							<button
								type="button"
								onclick={didClickGripperOpen}
								disabled={isMoving === true ||
									isTesting === true ||
									needsHome === true ||
									isHoming === true}
							>
								&#x2194;&#xFE0E;
							</button>
							<button
								type="button"
								onclick={didClickGripperClose}
								disabled={isMoving === true ||
									isTesting === true ||
									needsHome === true ||
									isHoming === true}
							>
								&#x2192;&#x2190;
							</button>
						{/if}
					</div>
				</td>
			</tr>
		</tbody>
	</table>

	{#if currentPosition !== null}
		<table class="status-table">
			<thead>
				<tr>
					<th colspan="3">cell configuration</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>
						<span class="field-label">x</span>
						<input
							type="number"
							step="0.5"
							bind:value={editX}
							disabled={isMoving === true || needsHome === true || isHoming === true}
						/>
						<button
							type="button"
							onclick={didClickMoveX}
							disabled={isMoving === true || needsHome === true || isHoming === true}
						>
							move
						</button>
					</td>
					<td>
						<span class="field-label">y</span>
						<input
							type="number"
							step="0.5"
							bind:value={editY}
							disabled={isMoving === true || needsHome === true || isHoming === true}
						/>
						<button
							type="button"
							onclick={didClickMoveY}
							disabled={isMoving === true || needsHome === true || isHoming === true}
						>
							move
						</button>
					</td>
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
				</tr>
			</tbody>
		</table>

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

		<div class="action-buttons">
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
			<button
				type="button"
				onclick={didClickSaveAndTest}
				disabled={isMoving === true ||
					isTesting === true ||
					isSaving === true ||
					needsHome === true ||
					isHoming === true}
			>
				save & test
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

	<div class="shelf-section">
		<ShelfCalibration
			disabled={needsHome === true || isHoming === true}
			on:needsHome={handleShelfNeedsHome}
		/>
	</div>

	<div class="section-divider"></div>

	<div class="action-buttons">
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
{/if}

<style>
	.shelf-section {
		margin-top: 2em;
		padding-top: 2em;
		border-top: 2px dashed #000;
	}

	.field-label {
		margin-right: 0.5em;
	}

	.calibrate-row {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.calibrate-button {
		margin-top: 0;
	}

	.status-table {
		margin-top: 1em;
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
		margin-top: 2em;
		align-items: center;
	}

	.section-divider {
		margin-top: 2em;
		border-top: 2px black dashed;
	}

	button {
		margin-top: 0;
	}

	input[type='number'] {
		width: 5em;
	}
</style>
