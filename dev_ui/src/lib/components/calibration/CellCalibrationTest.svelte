<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount, untrack } from 'svelte';
	import {
		ApiError,
		getCalibrationStatus,
		getStorageGrid,
		homeToolMotors,
		retrieveItem,
		runCalibrationCycleTest,
		runCalibrationQuickTest,
		storeItem,
		type ApiErrorDetail,
		type CalibrationStatus,
		type Cell,
		type StorageGrid
	} from '$lib/api';
	import { activeVocabulary, activeVocabularyStore, isSideToken, sideIndex, sideLabel } from '$lib/side';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	interface Props {
		// Accepts any side token the device vocabulary may use; normalized below.
		initialSide?: string;
		initialShelf?: number;
		initialCell?: number;
	}

	interface StoredCellSelection {
		// May hold a token from either vocabulary; normalized on restore.
		side?: string;
		shelf?: number;
		cell?: number;
	}

	let { initialSide, initialShelf, initialCell }: Props = $props();

	let storageGrid = $state<StorageGrid | null>(null);
	let loading = $state<boolean>(true);
	let loadError = $state<string>('');
	let vocabulary = $state(activeVocabulary());
	$effect(() => activeVocabularyStore.subscribe((v) => {
		vocabulary = v;
	}));

	// The initial side seeds the selection once; later prop changes must not reset it.
	const normalizedInitialSide = untrack(() =>
		initialSide !== undefined && sideIndex(initialSide) !== null
			? (sideIndex(initialSide) === 1 ? 'back_module' : 'front_module')
			: 'front_module'
	);

	let selectedSide = $state<'front_module' | 'back_module'>(normalizedInitialSide);
	let selectedShelf = $state<number>(0);
	let selectedCell = $state<number>(0);
	let isRunningQuick = $state<boolean>(false);
	let isRunningCycle = $state<boolean>(false);
	let isStoring = $state<boolean>(false);
	let isRetrieving = $state<boolean>(false);
	let isHoming = $state<boolean>(false);
	let error = $state<string>('');
	let message = $state<string>('');
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);

	const TOOL_AXES = new Set(['z_axis', 'stage']);
	const UNTESTABLE_CELL_STATUSES = new Set(['disabled', 'maintenance']);
	const TEST_SELECTION_STORAGE_KEY = 'opshelf.calibration.test.selection';

	const needsToolHome = $derived(needsHomeAxes.some((axis) => TOOL_AXES.has(axis)));
	const homeMessage = $derived(
		needsHome === true ? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}` : ''
	);
	const isBusy = $derived(
		isRunningQuick === true ||
		isRunningCycle === true ||
		isStoring === true ||
		isRetrieving === true ||
		isHoming === true ||
		loading === true
	);
	const homingDisabled = $derived(isBusy === true || needsToolHome === false);
	const selectedCellData = $derived(getSelectedCell());
	const isSelectedCellTestable = $derived(
		selectedCellData !== null && isCellTestable(selectedCellData) === true
	);
	const blockedCellMessage = $derived(
		selectedCellData !== null && isSelectedCellTestable === false
			? `cell ${selectedCellData.cell_idx} is ${selectedCellData.status}; select another cell to run tests`
			: ''
	);
	const actionsDisabled = $derived(
		isBusy === true || needsHome === true || isSelectedCellTestable === false
	);

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

		if (initialSide === undefined && storedSelection.side !== undefined && sideIndex(storedSelection.side) !== null) {
			selectedSide = sideIndex(storedSelection.side) === 1 ? 'back_module' : 'front_module';
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

	function isStorageModuleSide(value: unknown): boolean {
		return isSideToken(value) === true;
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

	function getSelectedCell(): Cell | null {
		const cells = getCells();

		return cells.find((cell) => cell.cell_idx === selectedCell) ?? null;
	}

	function isCellTestable(cell: Cell): boolean {
		return UNTESTABLE_CELL_STATUSES.has(cell.status.toLowerCase()) === false;
	}

	function findCellSelection(cells: Cell[], preferredCellIdx: number): Cell {
		const preferredCell = cells.find((cell) => cell.cell_idx === preferredCellIdx);

		if (preferredCell !== undefined && isCellTestable(preferredCell) === true) {
			return preferredCell;
		}

		return cells.find((cell) => isCellTestable(cell) === true) ?? preferredCell ?? cells[0];
	}

	function buildMockStoreItem(cell: Cell): Record<string, unknown> {
		const suffix = `${Date.now()}-${Math.floor(Math.random() * 1000000)}`;

		return {
			barcode: `CAL-${suffix}`,
			name: `calibration test ${selectedSide}:${selectedShelf}:${selectedCell}`,
			dimensions: {
				length: Math.max(cell.dimensions.length * 0.8, 1.0),
				width: Math.max(cell.dimensions.width * 0.8, 1.0),
				height: Math.max(cell.dimensions.height * 0.8, 1.0)
			}
		};
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

		selectedCell = findCellSelection(matchingShelf.cells, nextCell).cell_idx;
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'front_module' | 'back_module';
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

	async function didClickStore(): Promise<void> {
		if (selectedCellData === null) {
			error = 'selected cell not found';
			return;
		}

		isStoring = true;
		error = '';
		message = '';

		try {
			await storeItem(selectedSide, selectedShelf, selectedCell, buildMockStoreItem(selectedCellData));
			await didLoadStorage();
			await didLoadCalibrationStatus();
			message = 'store command requested';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to request store command';
		} finally {
			isStoring = false;
		}
	}

	async function didClickRetrieve(): Promise<void> {
		isRetrieving = true;
		error = '';
		message = '';

		try {
			await retrieveItem(selectedSide, selectedShelf, selectedCell);
			await didLoadStorage();
			await didLoadCalibrationStatus();
			message = 'retrieve command requested';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to request retrieve command';
		} finally {
			isRetrieving = false;
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
						<option value="front_module">{sideLabel(0, vocabulary).toLowerCase()} module</option>
						<option value="back_module">{sideLabel(1, vocabulary).toLowerCase()} module</option>
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
							<option value={cell.cell_idx} disabled={isCellTestable(cell) === false}>
								{cell.cell_idx}{isCellTestable(cell) === false ? ` (${cell.status})` : ''}
							</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="action-row">
						<button type="button" onclick={didClickQuick} disabled={actionsDisabled}>quick</button>
						<button type="button" onclick={didClickCycle} disabled={actionsDisabled}>cycle</button>
						<button type="button" onclick={didClickStore} disabled={actionsDisabled}>STORE</button>
						<button type="button" onclick={didClickRetrieve} disabled={actionsDisabled}>
							RETRIEVE
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>
{/if}

{#if blockedCellMessage !== ''}
	<StatusMessage message={blockedCellMessage} />
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
