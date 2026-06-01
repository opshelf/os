<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		ApiError,
		getCalibrationStatus,
		getStorageGrid,
		homeToolMotors,
		runCalibrationCycleTest,
		runCalibrationQuickTest,
		type ApiErrorDetail,
		type CalibrationStatus,
		type StorageGrid
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	interface Props {
		initialSide?: 'left_module' | 'right_module';
		initialShelf?: number;
		initialCell?: number;
	}

	interface StoredCellSelection {
		side?: 'left_module' | 'right_module';
		shelf?: number;
		cell?: number;
	}

	let { initialSide, initialShelf, initialCell }: Props = $props();

	let storageGrid = $state<StorageGrid | null>(null);
	let loading = $state<boolean>(true);
	let loadError = $state<string>('');
	let selectedSide = $state<'left_module' | 'right_module'>(initialSide ?? 'left_module');
	let selectedShelf = $state<number>(0);
	let selectedCell = $state<number>(0);
	let isRunningQuick = $state<boolean>(false);
	let isRunningCycle = $state<boolean>(false);
	let isHoming = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);

	const TOOL_AXES = new Set(['z_axis', 'stage']);
	const TEST_SELECTION_STORAGE_KEY = 'opshelf.calibration.test.selection';

	const needsToolHome = $derived(needsHomeAxes.some((axis) => TOOL_AXES.has(axis)));
	const homeMessage = $derived(
		needsHome === true ? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}` : ''
	);
	const isBusy = $derived(
		isRunningQuick === true || isRunningCycle === true || isHoming === true || loading === true
	);
	const homingDisabled = $derived(isBusy === true || needsToolHome === false);

	onMount(function () {
		if (browser === false) {
			return;
		}

		restoreSelection();
		void didLoad();
	});

	async function didLoad(): Promise<void> {
		await Promise.all([didLoadStorage(), didLoadCalibrationStatus()]);
	}

	async function didLoadStorage(): Promise<void> {
		loading = true;
		loadError = '';

		try {
			storageGrid = await getStorageGrid();
			syncSelection(initialShelf ?? selectedShelf, initialCell ?? selectedCell);
			persistSelection();
		} catch (err) {
			handleNeedsHomeError(err);
			loadError = err instanceof Error ? err.message : 'failed to load storage grid';
		} finally {
			loading = false;
		}
	}

	function restoreSelection(): void {
		const storedSelection = readStoredSelection();

		if (storedSelection === null) {
			return;
		}

		if (initialSide === undefined && storedSelection.side !== undefined) {
			selectedSide = storedSelection.side;
		}

		if (initialShelf === undefined && storedSelection.shelf !== undefined) {
			selectedShelf = storedSelection.shelf;
		}

		if (initialCell === undefined && storedSelection.cell !== undefined) {
			selectedCell = storedSelection.cell;
		}
	}

	function readStoredSelection(): StoredCellSelection | null {
		try {
			const storedSelection = window.localStorage.getItem(TEST_SELECTION_STORAGE_KEY);

			if (storedSelection === null) {
				return null;
			}

			const parsedSelection = JSON.parse(storedSelection) as StoredCellSelection;

			return {
				side: isStorageModuleSide(parsedSelection.side) === true ? parsedSelection.side : undefined,
				shelf: isSelectionIndex(parsedSelection.shelf) === true ? parsedSelection.shelf : undefined,
				cell: isSelectionIndex(parsedSelection.cell) === true ? parsedSelection.cell : undefined
			};
		} catch {
			return null;
		}
	}

	function persistSelection(): void {
		try {
			window.localStorage.setItem(
				TEST_SELECTION_STORAGE_KEY,
				JSON.stringify({
					side: selectedSide,
					shelf: selectedShelf,
					cell: selectedCell
				})
			);
		} catch {
			return;
		}
	}

	function isStorageModuleSide(value: unknown): value is 'left_module' | 'right_module' {
		return value === 'left_module' || value === 'right_module';
	}

	function isSelectionIndex(value: unknown): value is number {
		return typeof value === 'number' && Number.isInteger(value) === true && value >= 0;
	}

	async function didLoadCalibrationStatus(): Promise<void> {
		try {
			const status = await getCalibrationStatus();
			applyNeedsHomeStatus(status);
		} catch {
			return;
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

	function getShelvesForSide(side: 'left_module' | 'right_module') {
		if (storageGrid === null) {
			return [];
		}

		if (side === 'left_module') {
			return storageGrid.left_module;
		}

		return storageGrid.right_module;
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

		selectedSide = target.value as 'left_module' | 'right_module';
		syncSelection();
		persistSelection();
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		syncSelection(selectedShelf);
		persistSelection();
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = parseInt(target.value, 10);
		persistSelection();
		error = '';
		message = '';
	}

	async function didClickQuick(): Promise<void> {
		isRunningQuick = true;
		error = '';
		message = '';

		try {
			await runCalibrationQuickTest(selectedSide, selectedShelf, selectedCell);
			await didLoadCalibrationStatus();
			message = 'quick test completed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to run quick test';
		} finally {
			isRunningQuick = false;
		}
	}

	async function didClickCycle(): Promise<void> {
		isRunningCycle = true;
		error = '';
		message = '';

		try {
			await runCalibrationCycleTest(selectedSide, selectedShelf, selectedCell);
			await didLoadStorage();
			await didLoadCalibrationStatus();
			message = 'cycle test completed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to run cycle test';
		} finally {
			isRunningCycle = false;
		}
	}

	async function didClickHomeToolMotors(): Promise<void> {
		isHoming = true;
		error = '';
		message = '';

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
						{#each getShelves() as shelf}
							<option value={shelf.shelf_idx}>{shelf.shelf_idx}</option>
						{/each}
					</select>
				</td>
				<td>
					<span class="field-label">cell</span>
					<select value={selectedCell} onchange={didChangeCell}>
						{#each getCells() as cell}
							<option value={cell.cell_idx}>{cell.cell_idx}</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="action-row">
						<button type="button" onclick={didClickQuick} disabled={isBusy === true || needsHome === true}>
							quick
						</button>
						<button type="button" onclick={didClickCycle} disabled={isBusy === true || needsHome === true}>
							cycle
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>
{/if}

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

{#if needsHome === true && needsToolHome === true}
	<button type="button" onclick={didClickHomeToolMotors} disabled={homingDisabled}>home tool motors</button>
	<ErrorMessage message={homeMessage} />
{:else if needsHome === true}
	<ErrorMessage message={homeMessage} />
	<StatusMessage message="other motors require homing; use the motors page" />
{/if}

<style>
	.field-label {
		display: block;
		font-weight: 600;
		margin-bottom: 0.25rem;
	}

	.action-row {
		display: flex;
		gap: 0.75rem;
	}
</style>
