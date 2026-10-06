<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import {
		getStorageGrid,
		addShelf,
		addCell,
		deleteShelf,
		deleteCell,
		updateGridCells,
		type GridCellBatchItem,
		getBins,
		updateTrayLayout,
		assignTray,
		unassignTray,
		runCalibrationQuickTest,
		runCalibrationCycleTest,
		storeItem,
		retrieveItem,
		moveMotor,
		type StorageBin,
		type StorageModuleSide,
		type StorageGrid,
		type Shelf,
		type Cell
	} from '$lib/api';
	import { activeVocabulary, activeVocabularyStore, sideIndex, sideInitial, sideLabel } from '$lib/side';
	import type { SideIndex, SideVocabulary } from '$lib/side';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import { postToast } from '$lib/toasts';
	import {
		createReconnectingEventSource,
		type ReconnectingEventSource
	} from '$lib/sse';

	let vocabulary = $state(activeVocabulary());
	$effect(() => activeVocabularyStore.subscribe((v) => {
		vocabulary = v;
	}));

	let storageGrid = $state<StorageGrid | null>(null);
	let loading = $state<boolean>(true);
	let loadError = $state<string>('');
	let error = $state<string>('');
	let busy = $state<boolean>(false);

	let selectedKeys = $state<Set<string>>(new Set());
	let anchorSide = $state<StorageModuleSide | null>(null);
	let anchorShelf = $state<number | null>(null);
	let anchorCell = $state<number | null>(null);
	let overlayOpen = $state<boolean>(false);
	let scanStream: ReconnectingEventSource | null = null;

	// Multi-cell test walk (client-side equivalent of provision/scripts/cycle_test.py):
	// walks the testable cells on the current side from the selected cell, one
	// operation per cell, and reports the outcome as a single toast.
	let testRunning = $state<boolean>(false);
	let testAborting = $state<boolean>(false);
	let isMovingToCell = $state<boolean>(false);

	const selectedSide = $derived.by(
		(): StorageModuleSide =>
			sideIndex(page.url.searchParams.get('side')) === 1 ? 'back_module' : 'front_module'
	);

	const sideInitialLetter = $derived(sideInitialFor(selectedSide, vocabulary));

	function sideInitialFor(side: StorageModuleSide, vocab: SideVocabulary): string {
		return sideInitial(sideIndex(side) as SideIndex, vocab);
	}

	function sideHref(side: StorageModuleSide): string {
		const url = new URL(page.url.pathname, location.origin);
		url.searchParams.set('side', side);
		return url.pathname + url.search;
	}

	function getShelves(): Shelf[] {
		if (storageGrid === null) {
			return [];
		}
		const shelves =
			selectedSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;
		// Highest index renders on top, so a newly added shelf (next index) appears first.
		return [...shelves].sort((a, b) => b.shelf_idx - a.shelf_idx);
	}

	function cellKey(side: StorageModuleSide, shelfIdx: number, cellIdx: number): string {
		return `${side}:${shelfIdx}:${cellIdx}`;
	}

	function parseCellKey(key: string): { side: StorageModuleSide; shelf: number; cell: number } | null {
		const parts = key.split(':');
		if (parts.length !== 3) {
			return null;
		}
		const [side, shelf, cell] = parts;
		return { side: side as StorageModuleSide, shelf: parseInt(shelf, 10), cell: parseInt(cell, 10) };
	}

	const selectedLabels = $derived.by((): string[] => {
		const prefix = `${selectedSide}:`;
		const entries: Array<{ shelf: number; cell: number; label: string }> = [];

		for (const key of selectedKeys) {
			if (key.startsWith(prefix) === false) {
				continue;
			}
			const parsed = parseCellKey(key);
			if (parsed === null) {
				continue;
			}
			entries.push({ shelf: parsed.shelf, cell: parsed.cell, label: `${sideInitialLetter}:${parsed.shelf}:${parsed.cell}` });
		}

		entries.sort((a, b) => a.shelf - b.shelf || a.cell - b.cell);
		return entries.map((entry) => entry.label);
	});

	const selectionCount = $derived(selectedLabels.length);
	const hasSelection = $derived(selectionCount > 0);

	function isSelected(side: StorageModuleSide, shelfIdx: number, cellIdx: number): boolean {
		return selectedKeys.has(cellKey(side, shelfIdx, cellIdx));
	}

	function doesCellExist(shelfIdx: number, cellIdx: number): boolean {
		if (storageGrid === null) {
			return false;
		}
		const shelves =
			selectedSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;

		return shelves.some(
			(shelf) => shelf.shelf_idx === shelfIdx && shelf.cells.some((cell) => cell.cell_idx === cellIdx)
		);
	}

	function getShelfCells(shelfIdx: number): Shelf['cells'] | null {
		if (storageGrid === null) {
			return null;
		}
		const shelves =
			selectedSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;
		const shelf = shelves.find((candidate) => candidate.shelf_idx === shelfIdx);
		return shelf === undefined ? null : shelf.cells;
	}

	function setAnchor(shelfIdx: number, cellIdx: number | null): void {
		anchorSide = selectedSide;
		anchorShelf = shelfIdx;
		anchorCell = cellIdx;
	}

	// anchorCell === null means the anchor is a whole shelf (set by a shelf-label click).
	function getAnchor(): { shelf: number; cell: number | null } | null {
		if (anchorSide !== selectedSide || anchorShelf === null) {
			return null;
		}
		if (getShelfCells(anchorShelf) === null) {
			return null;
		}
		if (anchorCell !== null && doesCellExist(anchorShelf, anchorCell) === false) {
			return { shelf: anchorShelf, cell: null };
		}
		return { shelf: anchorShelf, cell: anchorCell };
	}

	function columnRange(anchorCell: number | null, anchorShelf: number, cellIdx: number): [number, number] {
		if (anchorCell !== null) {
			return [Math.min(anchorCell, cellIdx), Math.max(anchorCell, cellIdx)];
		}
		// Whole-shelf anchor: the rectangle starts at the anchor shelf's first cell.
		const cells = getShelfCells(anchorShelf) ?? [];
		const firstCell = cells.reduce(
			(min, cell) => Math.min(min, cell.cell_idx),
			Number.MAX_SAFE_INTEGER
		);
		if (firstCell === Number.MAX_SAFE_INTEGER) {
			// Anchor shelf has no cells: leave the column dimension unbounded.
			return [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER];
		}
		return [Math.min(firstCell, cellIdx), Math.max(firstCell, cellIdx)];
	}

	function buildRectangleSelection(
		fromShelf: number,
		toShelf: number,
		cellMin: number | undefined,
		cellMax: number | undefined
	): Set<string> {
		const shelfMin = Math.min(fromShelf, toShelf);
		const shelfMax = Math.max(fromShelf, toShelf);
		const next = new Set<string>();

		for (const shelf of getShelves()) {
			if (shelf.shelf_idx < shelfMin || shelf.shelf_idx > shelfMax) {
				continue;
			}
			for (const cell of shelf.cells) {
				const inColumns =
					cellMin === undefined ||
					cellMax === undefined ||
					(cell.cell_idx >= cellMin && cell.cell_idx <= cellMax);
				if (inColumns) {
					next.add(cellKey(selectedSide, shelf.shelf_idx, cell.cell_idx));
			}
		}
	}

		return next;
	}

	function didClickCell(
		event: Pick<MouseEvent, 'shiftKey' | 'ctrlKey' | 'metaKey'>,
		shelfIdx: number,
		cellIdx: number
	): void {
		const key = cellKey(selectedSide, shelfIdx, cellIdx);

		if (event.shiftKey === true) {
			const anchor = getAnchor();

			if (anchor !== null) {
				const [cellMin, cellMax] = columnRange(anchor.cell, anchor.shelf, cellIdx);
				selectedKeys = buildRectangleSelection(anchor.shelf, shelfIdx, cellMin, cellMax);
				setAnchor(shelfIdx, cellIdx);
				return;
			}
		}

		if (event.ctrlKey === true || event.metaKey === true) {
			const next = new Set(selectedKeys);
			if (next.has(key)) {
				next.delete(key);
			} else {
				next.add(key);
		}
			selectedKeys = next;
			setAnchor(shelfIdx, cellIdx);
			return;
		}

		if (selectedKeys.has(key) === true) {
			const next = new Set(selectedKeys);
			next.delete(key);
			selectedKeys = next;
		} else {
			selectedKeys = new Set([key]);
		}
		setAnchor(shelfIdx, cellIdx);
	}

	function didDoubleClickCell(shelfIdx: number, cellIdx: number): void {
		// A dblclick arrives after two click events, and the second click just toggled
		// the selection, so select this cell unconditionally.
		selectedKeys = new Set([cellKey(selectedSide, shelfIdx, cellIdx)]);
		setAnchor(shelfIdx, cellIdx);
		overlayOpen = true;
	}

	function didClickShelf(
		event: Pick<MouseEvent, 'shiftKey' | 'ctrlKey' | 'metaKey'>,
		shelfIdx: number
	): void {
		if (event.shiftKey === true) {
			const anchor = getAnchor();

			if (anchor !== null) {
				selectedKeys = buildRectangleSelection(anchor.shelf, shelfIdx, undefined, undefined);
				setAnchor(shelfIdx, null);
				return;
			}
		}

		const keys = (getShelfCells(shelfIdx) ?? []).map((cell) =>
			cellKey(selectedSide, shelfIdx, cell.cell_idx)
		);

		if (keys.length === 0) {
			setAnchor(shelfIdx, null);
			return;
		}

		const fullySelected = keys.every((candidate) => selectedKeys.has(candidate));

		if (event.ctrlKey === true || event.metaKey === true) {
			const next = new Set(selectedKeys);
			if (fullySelected) {
				for (const candidate of keys) {
					next.delete(candidate);
				}
			} else {
				for (const candidate of keys) {
				next.add(candidate);
			}
			}
			selectedKeys = next;
		} else if (fullySelected) {
			const next = new Set(selectedKeys);
			for (const candidate of keys) {
				next.delete(candidate);
		}
			selectedKeys = next;
		} else {
			selectedKeys = new Set(keys);
		}

		setAnchor(shelfIdx, null);
	}

	function didPressShelf(event: KeyboardEvent, shelfIdx: number): void {
		if (event.key !== 'Enter' && event.key !== ' ') {
			return;
		}
		event.preventDefault();
		didClickShelf(event, shelfIdx);
	}

	function didPressCell(event: KeyboardEvent, shelfIdx: number, cellIdx: number): void {
		if (event.key !== 'Enter' && event.key !== ' ') {
			return;
		}
		event.preventDefault();
		didClickCell(event, shelfIdx, cellIdx);
	}

	function clearCurrentSideSelection(): void {
		const prefix = `${selectedSide}:`;
		selectedKeys = new Set([...selectedKeys].filter((key) => key.startsWith(prefix) === false));

		if (anchorSide === selectedSide) {
			anchorSide = null;
			anchorShelf = null;
			anchorCell = null;
		}
	}

	function didEdit(): void {
		overlayOpen = true;
	}

	function didDeselect(): void {
		clearCurrentSideSelection();
		closeOverlay();
	}

	function closeOverlay(): void {
		overlayOpen = false;
	}

	// A scrim click only counts when the press both started and ended in the dimmed area.
	// The backdrop is pointer-events:none, so a drag that starts on the panel and ends in the
	// dimmed area (or vice versa) still dispatches one click whose target is a shared
	// ancestor; the mousedown and mouseup flags are what tell the two apart.
	let scrimPressInBounds = false;
	let scrimReleaseInBounds = false;

	function pressStartsInScrim(event: MouseEvent): boolean {
		if (overlayOpen === false) {
			return false;
		}
		const target = event.target instanceof Element ? event.target : null;
		if (target === null) {
			return false;
		}
		return target.closest('.selection-overlay') === null;
	}

	function didScrimMouseDown(event: MouseEvent): void {
		scrimPressInBounds = pressStartsInScrim(event);
	}

	function didScrimMouseUp(event: MouseEvent): void {
		scrimReleaseInBounds = pressStartsInScrim(event);
	}

	function didClickOutside(event: MouseEvent): void {
		const pressedInScrim = scrimPressInBounds;
		const releasedInScrim = scrimReleaseInBounds;
		scrimPressInBounds = false;
		scrimReleaseInBounds = false;
		if (overlayOpen === false) {
			return;
		}
		if (pressedInScrim === false || releasedInScrim === false) {
			return;
		}
		const target = event.target instanceof Element ? event.target : null;
		if (target === null) {
			return;
		}
		if (target.closest('.storage-cell') !== null) {
			return;
		}
		if (target.closest('.selection-overlay') !== null) {
			return;
		}
		if (target.closest('button, a') !== null) {
			return;
		}

		closeOverlay();
	}

	function didPressEscape(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || overlayOpen === false) {
			return;
		}
		closeOverlay();
	}

	type EditorField = 'status' | 'x' | 'y' | 'z' | 'width' | 'height' | 'length';
	type NumericEditorField = 'x' | 'y' | 'z' | 'width' | 'height' | 'length';
	type DimensionField = 'width' | 'height' | 'length';

	const EDITOR_FIELDS: EditorField[] = ['status', 'x', 'y', 'z', 'width', 'height', 'length'];
	const XYZ_COLUMNS: NumericEditorField[] = ['x', 'y', 'z'];
	const DIMENSION_COLUMNS: Array<{ field: DimensionField; label: string }> = [
		{ field: 'width', label: 'width' },
		{ field: 'height', label: 'height' },
		{ field: 'length', label: 'length' }
	];
	const CELL_STATUSES = ['available', 'occupied', 'maintenance', 'disabled'];

	let editorValues = $state<Record<EditorField, string>>({
		status: '',
		x: '',
		y: '',
		z: '',
		width: '',
		height: '',
		length: ''
	});

	const selectedCells = $derived.by(
		(): Array<{ shelfIdx: number; cell: Cell }> => {
			if (storageGrid === null) {
				return [];
			}
			const shelves =
				selectedSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;
			const entries: Array<{ shelfIdx: number; cell: Cell }> = [];

			for (const shelf of shelves) {
				for (const cell of shelf.cells) {
					if (selectedKeys.has(cellKey(selectedSide, shelf.shelf_idx, cell.cell_idx))) {
						entries.push({ shelfIdx: shelf.shelf_idx, cell });
					}
				}
		}

			return entries;
		}
	);

	// One value when every selected cell agrees, 'mixed' when they differ, null when none has one.
	function collapseValues(values: Array<number | string | null>): 'mixed' | number | string | null {
		if (values.length === 0) {
			return null;
		}
		const first = values[0];
		for (const value of values) {
			if (value !== first) {
				return 'mixed';
			}
		}
		return first;
	}

	const selectionValues = $derived.by(
		(): Record<EditorField, 'mixed' | number | string | null> => {
			const cells = selectedCells.map((entry) => entry.cell);
			const pointValues = (key: 'x' | 'y' | 'z_offset') =>
				cells.map((cell) => cell.calibration_point?.[key] ?? null);

			return {
				status: collapseValues(cells.map((cell) => cell.status)),
				x: collapseValues(pointValues('x')),
			y: collapseValues(pointValues('y')),
			z: collapseValues(pointValues('z_offset')),
			width: collapseValues(cells.map((cell) => cell.dimensions.width)),
			height: collapseValues(cells.map((cell) => cell.dimensions.height)),
			length: collapseValues(cells.map((cell) => cell.dimensions.length))
		};
		}
	);

	let bins = $state<StorageBin[]>([]);

	const selectedSingle = $derived(selectedCells.length === 1 ? selectedCells[0] : null);

	// The single selected cell's gantry target; null when none selected or uncalibrated.
	const moveTarget = $derived(
		selectedSingle === null ? null : selectedSingle.cell.calibration_point
	);

	const currentTray = $derived.by((): StorageBin | null => {
		if (selectedSingle === null || bins.length === 0) {
			return null;
		}
		const { shelfIdx, cell } = selectedSingle;
		for (const bin of bins) {
			if (!bin.active) {
				continue;
			}
			if (
				sideIndex(bin.home_location.side) === sideIndex(selectedSide) &&
				bin.home_location.shelf_idx === shelfIdx &&
				bin.home_location.cell_idx === cell.cell_idx
			) {
				return bin;
			}
		}
		return null;
	});

	let trayEnabled = $state<boolean>(false);
	let trayBarcode = $state<string>('');
	let trayRows = $state<string>('');
	let trayCols = $state<string>('');

	function trayDirty(): boolean {
		const current = currentTray;
		if (trayEnabled !== (current !== null)) {
			return true;
		}
		if (!trayEnabled || current === null) {
			return false;
		}
		if (trayBarcode.trim() !== current.barcode) {
			return true;
		}
		const layout = current.compartment_layout;
		const currentRows = layout !== null ? String(layout.rows) : '';
		const currentCols = layout !== null ? String(layout.columns) : '';
		return trayRows.trim() !== currentRows || trayCols.trim() !== currentCols;
	}

	function trayValid(): boolean {
		if (!trayEnabled) {
			return true;
		}
		if (trayBarcode.trim().length === 0 || trayBarcode.trim().length > 255) {
			return false;
		}
		const rows = Number(trayRows.trim());
		const columns = Number(trayCols.trim());
		if (!Number.isInteger(rows) || rows < 1 || rows > 32) {
			return false;
		}
		if (!Number.isInteger(columns) || columns < 1 || columns > 32) {
			return false;
		}
		return true;
	}

	function blankEditorValues(): Record<EditorField, string> {
		return { status: '', x: '', y: '', z: '', width: '', height: '', length: '' };
	}

	type LockValue = 'true' | 'false' | 'mixed';

	let lockValue = $state<LockValue>('false');
	let lockInput: HTMLInputElement | undefined = $state();

	const selectionIsManual = $derived.by((): 'mixed' | boolean => {
		const flags = selectedCells.map((entry) => entry.cell.is_manual === true);
		const first = flags[0];
		if (first === undefined) {
			return false;
		}
		return flags.every((flag) => flag === first) ? first : 'mixed';
	});

	$effect(() => {
		if (overlayOpen === false || hasSelection === false) {
			return;
		}
		const value = lockValue;
		const element = lockInput;
		if (element) {
			element.indeterminate = value === 'mixed';
		}
	});

	function normalizeLock(value: 'mixed' | boolean): LockValue {
		if (value === 'mixed') {
			return 'mixed';
		}
		return value ? 'true' : 'false';
	}

	function isLockDirty(): boolean {
		return lockValue !== normalizeLock(selectionIsManual);
	}

	function didToggleLock(): void {
		lockValue = lockInput ? (lockInput.checked ? 'true' : 'false') : 'false';
	}

	function populateEditorFromSelection(): void {
		const values = blankEditorValues();

		for (const field of EDITOR_FIELDS) {
			values[field] = displayForSelection(field);
		}
		editorValues = values;
		lockValue = normalizeLock(selectionIsManual);

		if (currentTray === null) {
			trayEnabled = false;
			trayBarcode = '';
			trayRows = '';
			trayCols = '';
		} else {
			trayEnabled = true;
			trayBarcode = currentTray.barcode;
			const layout = currentTray.compartment_layout;
			trayRows = layout !== null ? String(layout.rows) : '';
			trayCols = layout !== null ? String(layout.columns) : '';
		}
	}

	function didResetEditor(): void {
		populateEditorFromSelection();
	}

	function didFocusField(field: EditorField): void {
		if (field === 'status' || isMixedDisplay(field) === false) {
			return;
		}
		editorValues[field] = '';
	}

	function didBlurField(field: EditorField): void {
		if (field === 'status') {
			return;
		}
		if (editorValues[field].trim() === '' && selectionValues[field] === 'mixed') {
			editorValues[field] = 'Mixed';
		}
		// An out-of-range dimension is reported once the technician leaves the field, so
		// typing a value like ".5" does not fire a toast halfway through.
		if (isDimensionField(field) === true) {
			const issue = dimensionFieldIssue(field);
			if (issue !== '') {
				postToast('error', issue);
			}
		}
	}

	const selectionFingerprint = $derived(
		selectedCells
			.map((entry) => {
				const cell = entry.cell;
				const point = cell.calibration_point;
				return [
					entry.shelfIdx,
					cell.cell_idx,
					cell.status,
					cell.is_manual === true,
					point?.x ?? null,
					point?.y ?? null,
					point?.z_offset ?? null,
				cell.dimensions.width,
				cell.dimensions.height,
				cell.dimensions.length
			].join('|');
		})
			.join(';') +
			(currentTray === null
				? ''
				: `|tray:${currentTray.barcode}:${currentTray.compartment_layout?.rows ?? 0}:${currentTray.compartment_layout?.columns ?? 0}`)
	);

	// A new selection (or a reloaded grid under the same selection) starts the form clean.
	$effect(() => {
		if (selectionFingerprint === '') {
			return;
		}
		populateEditorFromSelection();
	});

	function formatFieldValue(value: number): string {
		return String(parseFloat(value.toFixed(4)));
	}

	function displayForSelection(field: EditorField): string {
		const value = selectionValues[field];

		if (field === 'status') {
			return value === 'mixed' ? 'mixed' : String(value);
		}
		if (value === 'mixed') {
			return 'Mixed';
		}
		if (value === null) {
			return '';
		}
		return typeof value === 'number' ? formatFieldValue(value) : String(value);
	}

	function isFieldDirty(field: EditorField): boolean {
		const raw = editorValues[field];
		const value = selectionValues[field];

		if (field === 'status') {
			if (raw === '') {
				return false;
			}
			if (value === 'mixed') {
				return raw.toLowerCase() !== 'mixed';
			}
			return raw !== String(value);
		}

		const trimmed = raw.trim();
		if (trimmed === '') {
			return false;
		}
		if (trimmed.toLowerCase() === 'mixed') {
			return false;
		}
		const parsed = Number(trimmed);
		if (Number.isFinite(parsed) === false) {
			return false;
		}
		if (value === 'mixed' || value === null) {
			return true;
		}
		if (typeof value !== 'number') {
			return true;
		}
		return formatFieldValue(parsed) !== formatFieldValue(value);
	}

	function isDimensionField(field: NumericEditorField): field is DimensionField {
		return field === 'width' || field === 'height' || field === 'length';
	}

	function dimensionFieldIssue(field: DimensionField): string {
		const raw = editorValues[field].trim();
		if (raw === '' || raw.toLowerCase() === 'mixed') {
			return '';
		}
		const parsed = Number(raw);
		if (Number.isFinite(parsed) === false) {
			return `${field} must be a number`;
		}
		if (parsed <= 0) {
			return `${field} must be greater than zero`;
		}
		return '';
	}

	function isFieldValid(field: EditorField): boolean {
		const raw = editorValues[field].trim();
		if (raw === '' || raw.toLowerCase() === 'mixed' || field === 'status') {
			return true;
		}
		const parsed = Number(raw);
		if (Number.isFinite(parsed) === false) {
			return false;
		}
		if (field === 'width' || field === 'height' || field === 'length') {
			if (parsed <= 0) {
				return false;
			}
		}
		return true;
	}

	const editorDirty = $derived(
		EDITOR_FIELDS.some((field) => isFieldDirty(field)) || isLockDirty() || trayDirty()
	);
	const editorValid = $derived(
		EDITOR_FIELDS.every((field) => isFieldValid(field)) && trayValid()
	);

	function isMixedDisplay(field: EditorField): boolean {
		const trimmed = editorValues[field].trim();
		return (
			selectionValues[field] === 'mixed' &&
			(trimmed === '' || trimmed.toLowerCase() === 'mixed')
		);
	}

	function editorNumeric(field: NumericEditorField, current: number): number {
		if (isFieldDirty(field) === true) {
			return Number(editorValues[field].trim());
		}
		return current;
	}

	async function didSaveEditor(): Promise<void> {
		if (busy === true || editorDirty === false || editorValid === false) {
			return;
		}

		busy = true;
		error = '';

		try {
			const xDirty = isFieldDirty('x');
			const yDirty = isFieldDirty('y');
			const zDirty = isFieldDirty('z');
			const coordinatesDirty = xDirty || yDirty || zDirty;
			const hasUncalibratedCell = selectedCells.some((entry) => entry.cell.calibration_point === null);
			if (coordinatesDirty === true && hasUncalibratedCell && (xDirty && yDirty && zDirty) === false) {
				error = 'x, y, and z are all required to set a calibration point on a cell without one';
				return;
			}
			const statusDirty = isFieldDirty('status') === true;
			const lockDirty = isLockDirty();
			const dimensionsDirty =
				isFieldDirty('width') || isFieldDirty('height') || isFieldDirty('length');

			const cells: GridCellBatchItem[] = selectedCells.map((entry) => {
				const cell = entry.cell;
				const point = cell.calibration_point;
				const item: GridCellBatchItem = {
					shelf_idx: entry.shelfIdx,
					cell_idx: cell.cell_idx
				};

				if (coordinatesDirty && (point !== null || (xDirty && yDirty && zDirty))) {
					item.x = editorNumeric('x', point?.x ?? 0);
					item.y = editorNumeric('y', point?.y ?? 0);
					item.z_offset = editorNumeric('z', point?.z_offset ?? 0);
				}
				if (statusDirty) {
					item.status = editorValues.status.trim();
				}
				if (lockDirty) {
				item.is_manual = lockValue === 'true';
				}
				if (dimensionsDirty) {
					item.dimensions = {
						width: editorNumeric('width', cell.dimensions.width),
						height: editorNumeric('height', cell.dimensions.height),
						length: editorNumeric('length', cell.dimensions.length)
					};
				}

				return item;
			});

			const gridDirty = coordinatesDirty || statusDirty || lockDirty || dimensionsDirty;

			if (gridDirty) {
				await updateGridCells(selectedSide, { cells });
			}

			if (selectedSingle !== null && trayDirty()) {
				const { shelfIdx, cell } = selectedSingle;
				const cellIdx = cell.cell_idx;
				const current = currentTray;
				const barcode = trayBarcode.trim();
				const rows = Number(trayRows.trim());
				const columns = Number(trayCols.trim());
				const layoutChanged =
				trayRows.trim() !== (current !== null && current.compartment_layout !== null
						? String(current.compartment_layout.rows)
						: '') ||
				trayCols.trim() !== (current !== null && current.compartment_layout !== null
					? String(current.compartment_layout.columns)
					: '');

				if (trayEnabled && current === null) {
					await assignTray(selectedSide, shelfIdx, cellIdx, { barcode, rows, columns });
			} else if (trayEnabled && current !== null) {
				if (barcode !== current.barcode) {
					await unassignTray(selectedSide, shelfIdx, cellIdx);
					await assignTray(selectedSide, shelfIdx, cellIdx, { barcode, rows, columns });
			} else if (layoutChanged) {
				await updateTrayLayout(selectedSide, shelfIdx, cellIdx, { rows, columns });
			}
		} else if (current !== null) {
			await unassignTray(selectedSide, shelfIdx, cellIdx);
		}
		}

			bins = (await getBins()).bins;
			storageGrid = await getStorageGrid();
		} catch (err) {
			fail(err, 'failed to save cells');
			// The save partially mutated server state (e.g. a barcode swap whose
			// unassign succeeded but assign was rejected); refresh so the overlay
			// reflects reality instead of the stale bins.
			try {
				bins = (await getBins()).bins;
				storageGrid = await getStorageGrid();
			} catch {
				/* best-effort refresh; keep the error set above */
			}
		} finally {
			busy = false;
		}
	}

	async function didDeleteSelectedCells(): Promise<void> {
		const label = selectionCount === 1 ? 'cell' : 'cells';
		const confirmed = confirm(
			`are you sure you want to delete the ${selectionCount} selected ${label}?`
		);
		if (confirmed !== true) {
			return;
		}

		busy = true;
		error = '';

		try {
			const targets = selectedCells.map((entry) => ({ shelfIdx: entry.shelfIdx, cellIdx: entry.cell.cell_idx }));

			for (const target of targets) {
				await deleteCell(selectedSide, target.shelfIdx, target.cellIdx);
			}
			storageGrid = await getStorageGrid();
			clearCurrentSideSelection();
			closeOverlay();
		} catch (err) {
			fail(err, 'failed to delete cells');
			// A partial failure deleted some cells already; reload + prune so the
			// deleted ones don't linger as ghosts and a retry isn't blocked by them.
			try {
				storageGrid = await getStorageGrid();
				pruneSelection();
			} catch {
				/* best-effort refresh; keep the error set above */
			}
		} finally {
			busy = false;
		}
	}

	async function didBulkDeleteSelected(): Promise<void> {
		if (busy === true || hasSelection === false || storageGrid === null) {
			return;
		}
		const shelves =
			selectedSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;
		const cellTargets: Array<{ shelf: number; cellIdx: number }> = [];
		const shelfTargets: number[] = [];

		for (const shelf of shelves) {
			const selectedOnShelf = shelf.cells.filter(
				(cell) => selectedKeys.has(cellKey(selectedSide, shelf.shelf_idx, cell.cell_idx))
			);
			if (selectedOnShelf.length === 0) {
				continue;
			}
			if (selectedOnShelf.length === shelf.cells.length) {
				shelfTargets.push(shelf.shelf_idx);
			} else {
				for (const cell of selectedOnShelf) {
					cellTargets.push({ shelf: shelf.shelf_idx, cellIdx: cell.cell_idx });
				}
			}
		}

		if (cellTargets.length === 0 && shelfTargets.length === 0) {
			return;
		}

		const parts: string[] = [];
		if (shelfTargets.length > 0) {
			const labels = [...shelfTargets].sort((a, b) => a - b).map((idx) => `${sideInitial}:${idx}`);
			parts.push(`${shelfTargets.length} ${shelfTargets.length === 1 ? 'shelf' : 'shelves'} ${labels.join(', ')}`);
		}
		if (cellTargets.length > 0) {
			const labels = cellTargets
				.map((target) => `${sideInitial}:${target.shelf}:${target.cellIdx}`)
				.sort();
			parts.push(`${cellTargets.length} ${cellTargets.length === 1 ? 'cell' : 'cells'} ${labels.join(', ')}`);
		}

		const confirmed = confirm(`are you sure you want to delete ${parts.join(' and ')}?`);
		if (confirmed !== true) {
			return;
		}

		busy = true;
		error = '';
		const deletedShelves: number[] = [];

		try {
			// Cell deletions keep the surviving cells' indices, so they run before any
			// shelf deletion shifts the shelf indices; shelves go down-to-up so the
			// indices of the shelves still to delete stay valid.
			for (const target of cellTargets) {
				await deleteCell(selectedSide, target.shelf, target.cellIdx);
			}
			for (const shelfIdx of [...shelfTargets].sort((a, b) => b - a)) {
				await deleteShelf(selectedSide, shelfIdx);
				deletedShelves.push(shelfIdx);
			}
		} catch (err) {
			fail(err, 'failed to delete');
		}

		try {
			storageGrid = await getStorageGrid();
		} catch (err) {
			fail(err, 'failed to load storage grid');
		}
		for (const shelfIdx of deletedShelves) {
			remapSelectionAfterShelfDelete(shelfIdx);
		}
		pruneSelection();
		if (error === '') {
			clearCurrentSideSelection();
			closeOverlay();
		}
		busy = false;
	}

	function pruneSelection(): void {
		const existing = new Set<string>();
		if (storageGrid !== null) {
			for (const shelf of storageGrid.front_module) {
				for (const cell of shelf.cells) {
					existing.add(cellKey('front_module', shelf.shelf_idx, cell.cell_idx));
				}
			}
			for (const shelf of storageGrid.back_module) {
				for (const cell of shelf.cells) {
					existing.add(cellKey('back_module', shelf.shelf_idx, cell.cell_idx));
				}
			}
		}

		const next = new Set([...selectedKeys].filter((key) => existing.has(key)));
		if (next.size !== selectedKeys.size) {
			selectedKeys = next;
		}
		if (next.size === 0) {
			closeOverlay();
		}
		if (anchorSide !== null && anchorShelf !== null && storageGrid !== null) {
			const anchorShelves =
				anchorSide === 'front_module' ? storageGrid.front_module : storageGrid.back_module;
			const surviving = anchorShelves.find((shelf) => shelf.shelf_idx === anchorShelf);

			if (surviving === undefined) {
				anchorSide = null;
				anchorShelf = null;
				anchorCell = null;
			} else if (
				anchorCell !== null &&
			surviving.cells.some((cell) => cell.cell_idx === anchorCell) === false
			) {
				anchorCell = null;
			}
		}
	}

	// Deleting shelf k reindexes old shelf k+1 to k on the backend, so a surviving
	// key `side:k:c` now denotes a different physical shelf and must be remapped
	// rather than pruned by existence (pruneSelection).
	function remapSelectionAfterShelfDelete(deletedShelfIdx: number): void {
		const next = new Set<string>();
		for (const key of selectedKeys) {
			const parsed = parseCellKey(key);
			if (parsed === null || parsed.side !== selectedSide) {
				next.add(key);
				continue;
			}
			if (parsed.shelf === deletedShelfIdx) {
				continue;
			}
			const newShelf = parsed.shelf > deletedShelfIdx ? parsed.shelf - 1 : parsed.shelf;
			next.add(cellKey(parsed.side, newShelf, parsed.cell));
		}
		selectedKeys = next;

		if (anchorSide === selectedSide && anchorShelf !== null) {
			if (anchorShelf === deletedShelfIdx) {
				anchorSide = null;
				anchorShelf = null;
				anchorCell = null;
			} else if (anchorShelf > deletedShelfIdx) {
				anchorShelf = anchorShelf - 1;
			}
		}

		if (next.size === 0) {
			closeOverlay();
		}
	}

	function connectScanStream(): void {
		if (scanStream !== null) {
			return;
		}
		const stream = createReconnectingEventSource({
			path: '/robot/events',
			onMessage: (event) => {
				try {
					const data = JSON.parse(event.data);
					if (data.kind === 'label_scanned' && typeof data.payload === 'string') {
						didReceiveScannedLabel(data.payload);
						return;
				}
					didReceiveCommandResult(data);
			} catch {
				return;
			}
		}
		});
		scanStream = stream;
		stream.connect();
	}

	function disconnectScanStream(): void {
		scanStream?.close();
		scanStream = null;
	}

	function didReceiveScannedLabel(barcode: string): void {
		if (overlayOpen && selectedSingle !== null && trayEnabled) {
			trayBarcode = barcode;
		}
	}

	type TestMode = 'quick' | 'cycle' | 'store' | 'retrieve';
	type TestCommandResult = { ok: boolean; error?: string };
	type TestCommandWait = { commandId: string; resolve: (result: TestCommandResult) => void };
	const UNTESTABLE_CELL_STATUSES = new Set(['disabled', 'maintenance']);
	const TEST_COMMAND_TIMEOUT_MS = 120000;

	// Resolved when the /robot/events stream delivers the result for a queued command.
	let pendingTestCommand: TestCommandWait | null = null;
	let mockStoreSeq = 0;

	function didReceiveCommandResult(data: Record<string, unknown>): void {
		if (pendingTestCommand === null) {
			return;
		}
		if (typeof data.command_id !== 'string' || data.command_id !== pendingTestCommand.commandId) {
			return;
		}
		const wait = pendingTestCommand;
		pendingTestCommand = null;
		const ok = data.message === 'success';
		const error = ok
			? undefined
			: typeof data.error === 'string' ? data.error : String(data.message ?? 'command failed');
		wait.resolve({ ok, error });
	}

	function waitForTestCommand(commandId: string): Promise<TestCommandResult> {
		return new Promise((resolve) => {
			pendingTestCommand = { commandId, resolve };
			setTimeout(() => {
				if (pendingTestCommand !== null && pendingTestCommand.commandId === commandId) {
					pendingTestCommand = null;
					resolve({ ok: false, error: 'timed out waiting for the command result' });
			}
			}, TEST_COMMAND_TIMEOUT_MS);
		});
	}

	// Laser calibration leaves cells 0x0x0 until a technician enters the real dimensions.
	function isCellUnsized(cell: Cell): boolean {
		return Math.min(cell.dimensions.width, cell.dimensions.height, cell.dimensions.length) <= 0;
	}

	function isCellTestable(cell: Cell): boolean {
		return UNTESTABLE_CELL_STATUSES.has(cell.status.toLowerCase()) === false;
	}

	// The backend rejects every testing action on disabled and maintenance cells, so the
	// testing buttons go inert when none of the selected cells can be tested. A mixed
	// selection stays enabled: the queue skips its untestable cells.
	const hasTestableSelection = $derived(selectedCells.some((entry) => isCellTestable(entry.cell)));

	// The reason the testing buttons are inert is announced once per selection through the
	// toast channel, which took over the overlay's inline message labels.
	let didAnnounceUntestableSelection = false;

	$effect(() => {
		if (hasTestableSelection === true || overlayOpen === false) {
			didAnnounceUntestableSelection = false;
			return;
		}

		if (didAnnounceUntestableSelection === false) {
			didAnnounceUntestableSelection = true;
			postToast('status', 'no testable cells selected');
		}
	});

	// The selected testable cells, one test each, in shelf/cell-ascending order
	// (cycle_test.py walks cells one at a time).
	function buildTestQueue(): Array<{ shelf: number; cell: Cell }> {
		if (storageGrid === null) {
			return [];
		}
		return selectedCells
			.filter((entry) => isCellTestable(entry.cell))
			.map((entry) => ({ shelf: entry.shelfIdx, cell: entry.cell }))
			.sort((a, b) => a.shelf - b.shelf || a.cell.cell_idx - b.cell.cell_idx);
	}

	function buildMockStoreItem(cell: Cell): Record<string, unknown> {
		mockStoreSeq += 1;
		// Keep it under the mock cabinet's fixed 300 mm cell and strictly inside the
		// real cell, so the item fits in both mock mode and on hardware.
		const scale = (value: number): number => Math.min(290, Math.max(value * 0.8, 1.0));
		return {
			barcode: String(Date.now()).slice(-12),
			name: `layout test ${mockStoreSeq}`,
			dimensions: {
				length: scale(cell.dimensions.length),
				width: scale(cell.dimensions.width),
			height: scale(cell.dimensions.height)
		}
		};
	}

	async function runCellTest(mode: TestMode, target: { shelf: number; cell: Cell }): Promise<TestCommandResult> {
		const { shelf, cell } = target;
		try {
			if (mode === 'quick') {
				await runCalibrationQuickTest(selectedSide, shelf, cell.cell_idx);
				return { ok: true };
			}
			if (mode === 'cycle') {
				await runCalibrationCycleTest(selectedSide, shelf, cell.cell_idx);
				return { ok: true };
		}
			if (mode === 'store') {
				const receipt = await storeItem(selectedSide, shelf, cell.cell_idx, buildMockStoreItem(cell));
				return await waitForTestCommand(receipt.command_id);
			}
			const receipt = await retrieveItem(selectedSide, shelf, cell.cell_idx);
			return await waitForTestCommand(receipt.command_id);
		} catch (err) {
			return { ok: false, error: err instanceof Error ? err.message : String(err) };
		}
	}

	async function didStartTest(mode: TestMode): Promise<void> {
		if (testRunning || hasSelection === false) {
			return;
		}
		const queue = buildTestQueue();
		if (queue.length === 0) {
			postToast('error', 'no testable cells selected');
			return;
		}
		testRunning = true;
		testAborting = false;
		try {
			for (const target of queue) {
				if (testAborting) {
					break;
				}
				const cellLabel = `${sideInitialLetter}:${target.shelf}:${target.cell.cell_idx}`;
				const result = await runCellTest(mode, target);
				if (result.ok === false) {
					postToast('error', `failed at ${cellLabel}${result.error ? `: ${result.error}` : ''}`);
					return;
				}
			}
			// One summary toast per walk: a failure returned above with its own toast.
			postToast('status', testAborting ? 'test aborted' : 'all tests passed');
		} finally {
			testRunning = false;
		}
	}

	function didAbortTest(): void {
		if (testRunning) {
			testAborting = true;
		}
	}

	// Gantry jog to the selected cell's calibrated x/y (z untouched). Hardware only:
	// 503 in mock mode, like the quick/cycle tests.
	async function didMoveToCell(): Promise<void> {
		if (isMovingToCell) {
			return;
		}
		const point = moveTarget;
		if (point === null) {
			return;
		}
		isMovingToCell = true;
		try {
			await moveMotor('x_axis', point.x);
			await moveMotor('y_axis', point.y);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			isMovingToCell = false;
		}
	}

	function filterDecimalInput(field: NumericEditorField, event: Event): void {
		const target = event.target as HTMLInputElement;
		const cleaned = target.value.replace(/[^0-9.\-]/g, '');
		if (cleaned !== target.value) {
			target.value = cleaned;
			editorValues[field] = cleaned;
		}
	}

	function filterDigitInput(event: Event, apply: (value: string) => void): void {
		const target = event.target as HTMLInputElement;
		const cleaned = target.value.replace(/[^0-9]/g, '');
		if (cleaned !== target.value) {
			target.value = cleaned;
			apply(cleaned);
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}
		void load();
		connectScanStream();
		document.addEventListener('mousedown', didScrimMouseDown);
		document.addEventListener('mouseup', didScrimMouseUp);
		document.addEventListener('click', didClickOutside);
		document.addEventListener('keydown', didPressEscape);
		return () => {
			disconnectScanStream();
			document.removeEventListener('mousedown', didScrimMouseDown);
			document.removeEventListener('mouseup', didScrimMouseUp);
			document.removeEventListener('click', didClickOutside);
			document.removeEventListener('keydown', didPressEscape);
		};
	});

	async function load(): Promise<void> {
		loading = true;
		loadError = '';
		try {
			const [grid, binList] = await Promise.all([getStorageGrid(), getBins()]);
			storageGrid = grid;
			bins = binList.bins;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'failed to load storage grid';
		} finally {
			loading = false;
		}
	}

	function fail(err: unknown, fallback: string): void {
		error = err instanceof Error ? err.message : fallback;
	}

	async function didAddShelf(): Promise<void> {
		busy = true;
		error = '';
		try {
			await addShelf(selectedSide);
			storageGrid = await getStorageGrid();
			pruneSelection();
		} catch (err) {
			fail(err, 'failed to add shelf');
		} finally {
			busy = false;
		}
	}

	// The 0:0 datum is the module's coordinate origin; z=13 is the fixed front
	// offset of the first shelf's opening.
	const ORIGIN_Z_OFFSET = 13;

	type InferredPoint = { cellIdx: number; x: number; y: number; z: number };

	// Infer the new (pointless) cell's calibration point from its calibrated
	// neighbours: a new 0:0 takes the fixed origin, a cell appended on a shelf
	// steps right from the nearest calibrated neighbour on that shelf
	// (accumulating the widths of any uncalibrated cells in between), and the
	// first cell of a higher shelf rises from the shelf below, keeping x and z.
	function inferNewCellPoint(shelfIdx: number): InferredPoint | null {
		const cells = getShelfCells(shelfIdx);
		if (cells === null || cells.length === 0) {
			return null;
		}
		const cellIdx = Math.max(...cells.map((cell) => cell.cell_idx));

		if (shelfIdx === 0 && cellIdx === 0) {
			return { cellIdx, x: 0, y: 0, z: ORIGIN_Z_OFFSET };
		}

		if (cellIdx === 0) {
			const belowCells = getShelfCells(shelfIdx - 1) ?? [];
			const reference =
				belowCells.find((cell) => cell.cell_idx === 0 && cell.calibration_point !== null) ??
				belowCells.find((cell) => cell.calibration_point !== null);
			if (reference === undefined || reference.calibration_point === null) {
				return null;
			}
			return {
				cellIdx,
				x: reference.calibration_point.x,
				y: reference.calibration_point.y + reference.dimensions.height,
				z: reference.calibration_point.z_offset
			};
		}

		const byIdx = new Map(cells.map((cell) => [cell.cell_idx, cell]));
		let widthSum = 0;
		for (let idx = cellIdx - 1; idx >= 0; idx -= 1) {
			const cell = byIdx.get(idx);
			if (cell === undefined) {
				continue; // gap left by a deleted cell: no physical slot, no width
			}
			if (cell.calibration_point !== null) {
				return {
					cellIdx,
					x: cell.calibration_point.x + widthSum + cell.dimensions.width,
					y: cell.calibration_point.y,
					z: cell.calibration_point.z_offset
				};
			}
			widthSum += cell.dimensions.width;
		}
		return null;
	}

	async function didAddCell(shelfIdx: number): Promise<void> {
		busy = true;
		error = '';
		try {
			await addCell(selectedSide, shelfIdx);
			storageGrid = await getStorageGrid();
			const inferred = inferNewCellPoint(shelfIdx);
			if (inferred !== null) {
				await updateGridCells(selectedSide, {
					cells: [
						{
							shelf_idx: shelfIdx,
							cell_idx: inferred.cellIdx,
							x: inferred.x,
							y: inferred.y,
							z_offset: inferred.z
						}
					]
				});
				storageGrid = await getStorageGrid();
			}
			pruneSelection();
		} catch (err) {
			fail(err, 'failed to add cell');
		} finally {
			busy = false;
		}
	}

	async function didDeleteShelf(shelfIdx: number): Promise<void> {
		const confirmed = confirm(
			`are you sure you want to delete shelf ${sideInitialLetter}:${shelfIdx}?`
		);
		if (confirmed !== true) {
			return;
		}

		busy = true;
		error = '';
		try {
			await deleteShelf(selectedSide, shelfIdx);
			storageGrid = await getStorageGrid();
			remapSelectionAfterShelfDelete(shelfIdx);
		} catch (err) {
			fail(err, 'failed to delete shelf');
		} finally {
			busy = false;
		}
	}
</script>

<div class="layout-toolbar">
	<div class="toggle-group">
		<a class:selected={selectedSide === 'front_module'} href={sideHref('front_module')}>
			{sideLabel(0, vocabulary).toLowerCase()}
		</a>
		<a class:selected={selectedSide === 'back_module'} href={sideHref('back_module')}>
			{sideLabel(1, vocabulary).toLowerCase()}
		</a>
	</div>
	<span class="toolbar-separator">—</span>
	<button type="button" onclick={didAddShelf} disabled={loading === true || busy === true}>
		add shelf
	</button>
	<button type="button" onclick={didEdit} disabled={hasSelection === false}>
		edit
	</button>
	<button type="button" onclick={didDeselect} disabled={hasSelection === false}>
		deselect
	</button>
	<button
		type="button"
		onclick={() => void didBulkDeleteSelected()}
		disabled={hasSelection === false || busy === true}
	>
		delete
	</button>
</div>

{#if loading === true}
	<p>loading storage grid...</p>
{:else if loadError !== ''}
	<ErrorMessage message={loadError} />
{:else if storageGrid !== null}
	{#if getShelves().length === 0}
		<p class="empty">no shelves on the {sideInitialLetter} side</p>
	{:else}
		<div class="shelf-list">
			{#each getShelves() as shelf (shelf.shelf_idx)}
				<div class="shelf-row">
					<span
				class="shelf-index"
				role="button"
				tabindex={0}
				onclick={(event) => didClickShelf(event, shelf.shelf_idx)}
				onkeydown={(event) => didPressShelf(event, shelf.shelf_idx)}
			>
				{sideInitialLetter}:{shelf.shelf_idx}
			</span>
					<div class="shelf-cells">
						{#each shelf.cells as cell (cell.cell_idx)}
							<div
								class="storage-cell"
								class:selected={isSelected(selectedSide, shelf.shelf_idx, cell.cell_idx)}
								class:occupied={cell.status.toLowerCase() === 'occupied'}
								class:unsized={isCellUnsized(cell)}
								class:dimmed={
									cell.status.toLowerCase() === 'maintenance' ||
									cell.status.toLowerCase() === 'disabled'
								}
								role="button"
								tabindex={0}
								onclick={(event) => didClickCell(event, shelf.shelf_idx, cell.cell_idx)}
								ondblclick={() => didDoubleClickCell(shelf.shelf_idx, cell.cell_idx)}
								onkeydown={(event) => didPressCell(event, shelf.shelf_idx, cell.cell_idx)}
							>
								{cell.cell_idx}
							</div>
						{/each}
					</div>
					<div class="shelf-actions">
						<button
							type="button"
							onclick={() => didAddCell(shelf.shelf_idx)}
							disabled={busy === true}
						>
							add cell
						</button>
						<button
							type="button"
							onclick={() => didDeleteShelf(shelf.shelf_idx)}
							disabled={busy === true}
						>
							delete shelf
						</button>
					</div>
				</div>
			{/each}
		</div>
	{/if}
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

{#if hasSelection && overlayOpen}
	<div class="selection-backdrop"></div>
	<aside class="selection-overlay">
		<p class="selection-count">
			{selectionCount} {selectionCount === 1 ? 'cell' : 'cells'} selected
		</p>
		<hr class="editor-hr" />
		{#if error !== ''}
			<ErrorMessage message={error} />
		{/if}
		<form
			class="cell-editor"
			onsubmit={(event) => {
				event.preventDefault();
				void didSaveEditor();
			}}
		>
			<label class="editor-row">
				<span>status</span>
				<select bind:value={editorValues.status} disabled={busy === true}>
					{#each CELL_STATUSES as status (status)}
						<option value={status}>{status}</option>
					{/each}
					{#if selectionValues.status === 'mixed'}
					<option value="mixed">mixed</option>
					{/if}
				</select>
			</label>
			<hr class="editor-hr" />
			<div class="editor-group">
				{#each XYZ_COLUMNS as field (field)}
					<label class="editor-col">
						<span>{field}</span>
						<input
							type="text"
						inputmode="decimal"
						class:mixed={isMixedDisplay(field)}
						bind:value={editorValues[field]}
						onfocus={() => didFocusField(field)}
						onblur={() => didBlurField(field)}
						oninput={(event) => filterDecimalInput(field, event)}
						disabled={busy === true}
					/>
					</label>
				{/each}
			</div>
			{#if selectedSingle !== null}
				<button
					type="button"
					class="editor-move-btn"
					onclick={() => void didMoveToCell()}
					disabled={isMovingToCell === true || moveTarget === null}
				>
					move to cell
				</button>
			{/if}
			<hr class="editor-hr" />
			<section class="editor-tests">
				<p class="editor-section-label">testing</p>
				<div class="editor-group editor-group-tests">
					<button
						type="button"
						onclick={() => void didStartTest('quick')}
						disabled={testRunning || hasTestableSelection === false}
					>
						quick
					</button>
					<button
						type="button"
						onclick={() => void didStartTest('cycle')}
						disabled={testRunning || hasTestableSelection === false}
					>
						cycle
					</button>
					<button
						type="button"
						onclick={() => void didStartTest('store')}
						disabled={testRunning || hasTestableSelection === false}
					>
						store
					</button>
					<button
						type="button"
						onclick={() => void didStartTest('retrieve')}
						disabled={testRunning || hasTestableSelection === false}
					>
						retrieve
					</button>
					{#if testRunning}
						<button
							type="button"
							class="editor-abort-button"
							onclick={() => didAbortTest()}
							disabled={testAborting}
						>
							abort test
						</button>
					{/if}
				</div>
			</section>
			<label class="editor-row editor-row-lock">
				<input
					type="checkbox"
					checked={lockValue === 'true'}
					bind:this={lockInput}
					onchange={didToggleLock}
					disabled={busy === true}
				/>
				<span>lock calibration</span>
			</label>
			<hr class="editor-hr" />
			<div class="editor-group">
				{#each DIMENSION_COLUMNS as column (column.field)}
					<label class="editor-col">
						<span>{column.label}</span>
						<input
							type="text"
						inputmode="decimal"
						class:mixed={isMixedDisplay(column.field)}
						bind:value={editorValues[column.field]}
						onfocus={() => didFocusField(column.field)}
						onblur={() => didBlurField(column.field)}
						oninput={(event) => filterDecimalInput(column.field, event)}
						disabled={busy === true}
					/>
					</label>
				{/each}
			</div>
			{#if selectedSingle !== null}
				<hr class="editor-hr" />
				<label class="editor-row editor-row-tray">
					<input
						type="checkbox"
						bind:checked={trayEnabled}
						disabled={busy === true}
				/>
					<span>multi-item tray</span>
				</label>
				{#if trayEnabled}
					<div class="editor-group editor-group-1">
						<label class="editor-col">
							<span>tray barcode</span>
							<input
								type="text"
								maxlength="255"
								bind:value={trayBarcode}
								disabled={busy === true}
						/>
						</label>
					</div>
					<div class="editor-group editor-group-2">
						<label class="editor-col">
							<span>rows</span>
							<input
								type="text"
								inputmode="numeric"
							bind:value={trayRows}
							oninput={(event) => filterDigitInput(event, (value) => { trayRows = value; })}
							disabled={busy === true}
						/>
						</label>
						<label class="editor-col">
							<span>columns</span>
							<input
							type="text"
						inputmode="numeric"
						bind:value={trayCols}
						oninput={(event) => filterDigitInput(event, (value) => { trayCols = value; })}
						disabled={busy === true}
					/>
					</label>
				</div>
				{/if}
			{/if}
			<div class="editor-spacer"></div>
		<hr class="editor-hr" />
			<div class="editor-actions">
				<button
					type="button"
				onclick={() => void didDeleteSelectedCells()}
				disabled={busy === true}
			>
				{selectionCount === 1 ? 'delete cell' : 'delete cells'}
			</button>
			<div class="editor-spacer"></div>
			<button
				type="button"
			onclick={() => didResetEditor()}
			disabled={busy === true || editorDirty === false}
			>
				reset
			</button>
			<button
				type="submit"
				disabled={busy === true || editorDirty === false || editorValid === false}
			>
				save
			</button>
		</div>
		</form>
	</aside>
{/if}

<style>
	.layout-toolbar {
		display: flex;
		gap: 1em;
		align-items: center;
		margin-bottom: 1.5em;
	}

	.toggle-group {
		display: inline-flex;
		gap: 1em;
		align-items: center;
	}

	.toggle-group a {
		border: 2px solid black;
		background: #fff;
		cursor: pointer;
		margin: 0;
		color: black;
		text-decoration: none;
	}

	.toggle-group a.selected {
		background: black;
		color: #fff;
	}

	.toolbar-separator {
		color: black;
	}

	.layout-toolbar button {
		margin-top: 0;
		min-width: 36px;
	}

	.empty {
		margin-bottom: 1.5em;
	}

	.shelf-list {
		display: flex;
		flex-direction: column;
		gap: 1em;
	}

	.shelf-row {
		display: flex;
		gap: 1em;
		align-items: center;
		flex-wrap: wrap;
		user-select: none;
	}

	.shelf-index {
		min-width: 3ch;
		text-align: right;
		cursor: pointer;
	}

	.shelf-cells {
		display: flex;
		gap: 1em;
	}

	.storage-cell {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		border: 2px solid black;
		background: #fff;
		cursor: pointer;
	}

	.storage-cell.occupied {
		background: rgba(0, 0, 0, 0.5);
	}

	.storage-cell.unsized {
		background: #fdd;
		border-color: #d00;
		color: #d00;
	}

	.storage-cell.dimmed {
		opacity: 0.5;
	}

	.storage-cell.selected {
		background: black;
		color: #fff;
		opacity: 1;
	}

	.shelf-actions {
		display: flex;
		gap: 1em;
		margin-left: auto;
	}

	.shelf-actions button {
		margin-top: 0;
		min-width: 36px;
	}

	.selection-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.4);
		pointer-events: none;
		z-index: 40;
	}

	.selection-overlay {
		position: fixed;
		top: 0;
		right: 0;
		bottom: 0;
		width: 50vw;
		margin: 1.5em;
		overflow-y: auto;
		padding: 1.5em;
		background: white;
		border: 2px solid black;
		z-index: 41;
		display: flex;
		flex-direction: column;
	}

	.selection-count {
		margin-bottom: 1.5em;
		text-transform: uppercase;
	}

	.cell-editor {
		flex: 1;
		display: flex;
		flex-direction: column;
	}

	.editor-spacer {
		flex: 1;
	}

	.editor-hr {
		flex: none;
		width: 100%;
		height: 0;
		margin: 0 0 1.5em 0;
		border: none;
		border-top: 2px solid black;
	}

	.editor-row {
		display: flex;
		gap: 1em;
		align-items: center;
		margin-bottom: 1.5em;
	}

	.editor-row span {
		min-width: 7ch;
		text-transform: uppercase;
	}

	.editor-row select {
		flex: 1;
		height: 2.5em;
		padding: 0.4em 0.6em;
		border: 2px solid black;
		background: #fff;
		font: inherit;
		text-transform: none;
		color: black;
	}

	.editor-group {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 1.5em;
		margin-bottom: 1.5em;
	}

	.editor-group-2 {
		grid-template-columns: repeat(2, 1fr);
	}

	.editor-group-1 {
		grid-template-columns: 1fr;
	}

	.editor-col {
		display: flex;
		flex-direction: column;
		gap: 0.3em;
	}

	.editor-col span {
		text-transform: uppercase;
	}

	.editor-col input {
		width: 100%;
		height: 2.5em;
		padding: 0.4em 0.6em;
		border: 2px solid black;
		background: #fff;
		font: inherit;
		color: black;
	}

	.editor-col input.mixed {
		color: #6b6b6b;
	}

	.editor-row-lock span {
		min-width: 0;
	}

	.editor-row-lock input {
		flex: none;
		width: 1.2em;
		height: 1.2em;
		margin: 0;
	}

	.editor-row-tray input {
		flex: none;
		width: 1.2em;
		height: 1.2em;
		margin: 0;
	}

	.editor-actions {
		display: flex;
		gap: 1em;
	}

	.editor-actions button {
		margin-top: 0;
	}

	.editor-move-btn {
		width: 100%;
		height: 2.5em;
		margin: 0 0 1.5em 0;
	}

	.editor-tests {
		display: flex;
		flex-direction: column;
	}

	.editor-section-label {
		text-transform: uppercase;
		margin-bottom: 1.5em;
	}

	.editor-group-tests {
		grid-template-columns: repeat(2, 1fr);
	}

	.editor-group-tests button {
		width: 100%;
		height: 2.5em;
		margin-top: 0;
	}

	.editor-group-tests button:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.editor-abort-button {
		grid-column: 1 / -1;
	}
</style>
