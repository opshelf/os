<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		getStorageGrid,
		getCalibrationStatus,
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
	import { robotStatus } from '$lib/stores';
	import { activeVocabulary, activeVocabularyStore, sideIndex, sideLabel } from '$lib/side';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	interface Props {
		// Accepts any side token the device vocabulary may use; normalized below.
		initialSide?: string;
		initialShelf?: number;
		initialCell?: number;
	}

	let { initialSide, initialShelf, initialCell }: Props = $props();

	let storageGrid = $state<StorageGrid | null>(null);
	let loading = $state<boolean>(true);
	let loadError = $state<string>('');

	let vocabulary = $state(activeVocabulary());
	$effect(() => activeVocabularyStore.subscribe((v) => {
		vocabulary = v;
	}));

	const normalizedInitialSide =
		initialSide !== undefined && sideIndex(initialSide) !== null
			? (sideIndex(initialSide) === 1 ? 'back_module' : 'front_module')
			: 'front_module';

	let selectedSide = $state<'front_module' | 'back_module'>(normalizedInitialSide);
	let selectedShelf = $state<number>(0);
	let selectedCell = $state<number>(0);
	let currentPosition = $state<Position | null>(null);
	let isMoving = $state<boolean>(false);
	let isTesting = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let warning = $state<string>('');
	let needsHome = $state<boolean>(false);
	let lastStreamNeedsHome = $state<boolean>(false);

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
			syncSelection(initialShelf ?? selectedShelf, initialCell ?? selectedCell);
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
	}

	function applyNeedsHomeDetail(detail?: ApiErrorDetail): void {
		if (detail?.needs_home !== true) {
			return;
		}
		needsHome = true;
	}

	function handleNeedsHomeError(err: unknown): void {
		if (err instanceof ApiError) {
			applyNeedsHomeDetail(err.detail);
		}
	}

	// Re-read /calibration/status when the shared stream reports the crashed
	// motor was homed, so the calibration controls unblock. Only the
	// true->false edge fetches; /calibration/status remains the source of truth.
	$effect(() => {
		const streamNeedsHome = $robotStatus?.needs_home ?? false;

		if (lastStreamNeedsHome === true && streamNeedsHome === false) {
			void loadCalibrationStatus();
		}

		lastStreamNeedsHome = streamNeedsHome;
	});

	function getShelvesForSide(side: 'front_module' | 'back_module') {
		if (storageGrid === null) {
			return [];
		}

		if (side === 'front_module') {
			return storageGrid.front_module;
		}

		return storageGrid.back_module;
	}

	function getShelves() {
		return getShelvesForSide(selectedSide);
	}

	function getSelectedShelf() {
		const shelves = getShelves();
		return shelves.find((shelf) => shelf.shelf_idx === selectedShelf) ?? null;
	}

	function getCells() {
		const shelf = getSelectedShelf();

		if (shelf === null) {
			return [];
		}

		return shelf.cells;
	}

	function getShelfSelectionValue(): string {
		return String(selectedShelf);
	}

	function getCellSelectionValue(): string {
		return String(selectedCell);
	}

	function getShelfOptionIndices(): number[] {
		const shelfIndices = getShelves().map((shelf) => shelf.shelf_idx);

		if (shelfIndices.includes(selectedShelf) === true) {
			return shelfIndices;
		}

		return [selectedShelf, ...shelfIndices];
	}

	function getCellOptionIndices(): number[] {
		const cellIndices = getCells().map((cell) => cell.cell_idx);

		if (cellIndices.includes(selectedCell) === true) {
			return cellIndices;
		}

		return [selectedCell, ...cellIndices];
	}

	function syncSelection(preferredShelf?: number, preferredCell?: number): void {
		const shelves = getShelvesForSide(selectedSide);

		if (shelves.length === 0) {
			selectedShelf = 0;
			selectedCell = 0;
			return;
		}

		const nextShelf = preferredShelf ?? selectedShelf;
		const matchingShelf = shelves.find((shelf) => shelf.shelf_idx === nextShelf) ?? shelves[0];

		selectedShelf = matchingShelf.shelf_idx;

		if (matchingShelf.cells.length === 0) {
			selectedCell = 0;
			return;
		}

		const nextCell = preferredCell ?? selectedCell;
		const matchingCell =
			matchingShelf.cells.find((cell) => cell.cell_idx === nextCell) ?? matchingShelf.cells[0];

		selectedCell = matchingCell.cell_idx;
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'front_module' | 'back_module';
		syncSelection();
		currentPosition = null;
		error = '';
		message = '';
		warning = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		syncSelection(selectedShelf);
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
			await controlGripper('release');
			storageGrid = await getStorageGrid();

			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);

			const rotationAngles = await getRotationAngles();
			const targetAngle =
				selectedSide === 'back_module' ? rotationAngles.back : rotationAngles.front;

			await rotateToAngle(targetAngle);
			await loadStageExtensionForSide();

			syncEditValues();
			message = 'moved to cell';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to start calibration';
		} finally {
			isMoving = false;
		}
	}

	async function loadStageExtensionForSide(): Promise<void> {
		try {
			const ext = await getShelfStageExtension();

			stageExtensionValue = selectedSide === 'front_module' ? ext.front : ext.back;
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
			const warnings = result.warnings ?? [];

			warning = warnings.length > 0 ? warnings.join('; ') : '';
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

	// Saving from this page always marks the cell manual, so it is excluded
	// from auto-calibration.
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
				currentPosition.z_offset,
				undefined,
				true
			);

			storageGrid = await getStorageGrid();
			syncSelection(selectedShelf, selectedCell);

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

	// Move back to the cell's last saved position, i.e. the state from before
	// this calibration session's (unsaved) changes.
	async function didClickRevert(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);

			syncEditValues();
			message = 'position reverted';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to revert position';
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
			message = 'gripper closed';
		} catch (err) {
			handleNeedsHomeError(err);
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
			message = 'gripper opened';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to release gripper';
		} finally {
			isMoving = false;
		}
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
	<div class="panel-row">
		<div class="panel-column">
			<table class="status-table">
				<thead>
					<tr>
						<th colspan="3">cell selection</th>
					</tr>
				</thead>
				<tbody>
					<tr>
						<td>
							<span>side</span>
							<select value={selectedSide} onchange={didChangeSide}>
								<option value="front_module">{sideLabel(0, vocabulary).toLowerCase()} module</option>
								<option value="back_module">{sideLabel(1, vocabulary).toLowerCase()} module</option>
							</select>
						</td>
						<td>
							<span>shelf</span>
							<select value={getShelfSelectionValue()} onchange={didChangeShelf}>
								{#each getShelfOptionIndices() as shelfIdx}
									<option value={String(shelfIdx)}>{shelfIdx}</option>
								{/each}
							</select>
						</td>
						<td>
							<span>cell</span>
							<select value={getCellSelectionValue()} onchange={didChangeCell}>
								{#each getCellOptionIndices() as cellIdx}
									<option value={String(cellIdx)}>{cellIdx}</option>
								{/each}
							</select>
						</td>
					</tr>
				</tbody>
			</table>

			<div class="action-buttons">
				<!-- CALIBRATE stays disabled while the selected cell is the one already being calibrated. -->
				<button
					type="button"
					onclick={didClickCalibrate}
					disabled={isMoving === true ||
						isTesting === true ||
						isExtending === true ||
						needsHome === true ||
						currentPosition !== null}
				>
					calibrate
				</button>
			</div>
		</div>

		{#if currentPosition !== null}
			<div class="panel-column">
				<table class="status-table">
					<thead>
						<tr>
							<th>controls</th>
						</tr>
					</thead>
					<tbody>
						<tr>
							<td>
								<div class="control-cell">
									<span>X</span>
									<input
										type="number"
										step="0.5"
										bind:value={editX}
										disabled={isMoving === true || needsHome === true}
									/>
									<button
										type="button"
										onclick={didClickMoveX}
										disabled={isMoving === true || needsHome === true}
									>
										move
									</button>
									<div class="spacer"></div>
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
									<div class="jog-step">
										<select bind:value={jogStep} disabled={needsHome === true}>
											{#each JOG_STEPS as step}
												<option value={step.value}>{step.label}</option>
											{/each}
										</select>
									</div>
								</div>
							</td>
						</tr>
						<tr>
							<td>
								<div class="control-cell">
									<span>Y</span>
									<input
										type="number"
										step="0.5"
										bind:value={editY}
										disabled={isMoving === true || needsHome === true}
									/>
									<button
										type="button"
										onclick={didClickMoveY}
										disabled={isMoving === true || needsHome === true}
									>
										move
									</button>
									<div class="spacer"></div>
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
									<div class="jog-step">
										<select bind:value={jogStep} disabled={needsHome === true}>
											{#each JOG_STEPS as step}
												<option value={step.value}>{step.label}</option>
											{/each}
										</select>
									</div>
								</div>
							</td>
						</tr>
						<tr>
							<td>
								<div class="control-cell">
									<span>Z</span>
									<input
										type="number"
										step="0.5"
										bind:value={editZ}
										disabled={isMoving === true || needsHome === true}
									/>
									<button
										type="button"
										onclick={didClickMoveZ}
										disabled={isMoving === true || needsHome === true}
									>
										move
									</button>
									<div class="spacer"></div>
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
									<div class="jog-step">
										<select bind:value={jogStep} disabled={needsHome === true}>
											{#each JOG_STEPS as step}
												<option value={step.value}>{step.label}</option>
											{/each}
										</select>
									</div>
								</div>
							</td>
						</tr>
						<tr>
							<td>
								<div class="step-buttons">
									<span>STAGE</span>
									<div class="spacer"></div>
									<button
										type="button"
										onclick={didClickExtend}
										disabled={isMoving === true ||
											isTesting === true ||
											isExtending === true ||
											needsHome === true ||
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
											needsHome === true}
									>
										retract
									</button>
								</div>
							</td>
						</tr>
						<tr>
							<td>
								<div class="step-buttons">
									<span>GRIPPER</span>
									<div class="spacer"></div>
									<button
										type="button"
										onclick={didClickGrip}
										disabled={isMoving === true || isTesting === true || needsHome === true}
									>
										grip
									</button>
									<button
										type="button"
										onclick={didClickRelease}
										disabled={isMoving === true || isTesting === true || needsHome === true}
									>
										release
									</button>
								</div>
							</td>
						</tr>
					</tbody>
				</table>

				<div class="action-buttons">
					<button
						type="button"
						onclick={didClickRevert}
						disabled={isMoving === true || isTesting === true || needsHome === true}
					>
						revert
					</button>
					<button
						type="button"
						onclick={didClickSaveAndTest}
						disabled={isMoving === true || isTesting === true || needsHome === true}
					>
						save & test
					</button>
				</div>
			</div>
		{/if}
	</div>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if warning !== ''}
		<StatusMessage message={`warning: ${warning}`} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	td,
	th {
		height: 51px;
	}

	table {
		max-width: 50vw;
	}

	.status-table {
		margin-top: 0.5em;
		width: 100%;
	}

	.panel-row {
		display: flex;
		gap: 1.5em;
		align-items: flex-start;
	}

	.panel-column {
		flex: 1;
	}

	.jog-step {
		display: flex;
		gap: 0.5em;
		align-items: center;
		margin-left: auto;
	}

	.control-cell {
		display: flex;
		gap: 0.5em;
		align-items: center;
	}

	.step-buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.spacer {
		flex: 1;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1.5em;
		align-items: center;
	}

	button {
		margin-top: 0;
		min-width: 36px;
	}

	input[type='number'] {
		width: 5em;
	}
</style>
