<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		getLaserAutoCalibrationStatus,
		getLaserAutoCalibrationStreamError,
		getStorageGrid,
		type LaserAutoCalibrationAngleResult,
		type LaserAutoCalibrationRequest,
		type LaserAutoCalibrationSnapshot,
		type StorageGrid
	} from '$lib/api';
	import { createReconnectingEventSource, type ReconnectingEventSource } from '$lib/sse';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let snapshot = $state<LaserAutoCalibrationSnapshot | null>(null);
	let storageGrid = $state<StorageGrid | null>(null);
	let error = $state<string>('');
	let message = $state<string>('');
	let isLoading = $state<boolean>(true);
	let isConnecting = $state<boolean>(false);
	let isStreamOpen = $state<boolean>(false);
	let isAborting = $state<boolean>(false);
	let isAllSelected = $state<boolean>(true);
	let isAngleCalibrationSelected = $state<boolean>(true);
	let isExtensionCalibrationSelected = $state<boolean>(true);
	let preferredAngleCalibration = $state<boolean>(true);
	let preferredExtensionCalibration = $state<boolean>(true);
	let selectedSide = $state<'' | 'front_module' | 'back_module'>('');
	let selectedShelf = $state<string>('');
	let selectedCell = $state<string>('');
	let activeRequestedScope = $state<LaserAutoCalibrationRequest | null>(null);
	let stream = $state<ReconnectingEventSource | null>(null);
	let isStreamSubscribeOnly = false;

	const isSessionActive = $derived.by(() => {
		if (snapshot === null) {
			return false;
		}

		return (
			snapshot.state === 'starting' || snapshot.state === 'running' || snapshot.state === 'applying'
		);
	});

	const isStreamReconnecting = $derived.by(() => {
		if (snapshot === null) {
			return false;
		}

		if (stream === null) {
			return false;
		}

		return shouldAutoConnect(snapshot) === true && isStreamOpen === false && isConnecting === false;
	});

	const isSelectionDisabled = $derived.by(() => {
		return (
			isSessionActive === true ||
			isConnecting === true ||
			isStreamOpen === true ||
			isStreamReconnecting === true
		);
	});

	const isAngleCalibrationDisabled = $derived.by(() => {
		return selectedShelf !== '' || isSelectionDisabled === true;
	});

	const isExtensionCalibrationDisabled = $derived.by(() => {
		return selectedShelf !== '' || isSelectionDisabled === true;
	});

	const isStartDisabled = $derived.by(() => {
		if (isConnecting === true || isStreamOpen === true || isStreamReconnecting === true) {
			return true;
		}

		if (isAllSelected === true) {
			return false;
		}

		return false;
	});

	const startButtonLabel = 'calibrate';
	const defaultRequestedScope: LaserAutoCalibrationRequest = {
		mode: 'all',
		include_angle_calibration: true,
		include_extension_calibration: true
	};

	onMount(function () {
		if (browser === false) {
			return;
		}

		void didLoad();

		return function () {
			didCloseStream();
		};
	});

	async function didLoad(): Promise<void> {
		isLoading = true;
		error = '';

		try {
			const [nextSnapshot, nextStorageGrid] = await Promise.all([
				getLaserAutoCalibrationStatus(),
				getStorageGrid()
			]);

			snapshot = nextSnapshot;

			if (nextSnapshot.requested_scope !== undefined) {
				activeRequestedScope = nextSnapshot.requested_scope;
			}

			storageGrid = nextStorageGrid;
			syncScopeSelection(getSnapshotRequestedScope(nextSnapshot));

			if (shouldAutoConnect(nextSnapshot) === true) {
				void didConnectStream(true);
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load auto calibration status';
		} finally {
			isLoading = false;
		}
	}

	function shouldAutoConnect(currentSnapshot: LaserAutoCalibrationSnapshot): boolean {
		return (
			currentSnapshot.state === 'starting' ||
			currentSnapshot.state === 'running' ||
			currentSnapshot.state === 'applying'
		);
	}

	function isTerminalSnapshot(currentSnapshot: LaserAutoCalibrationSnapshot | null): boolean {
		if (currentSnapshot === null) {
			return false;
		}

		return (
			currentSnapshot.state === 'completed' ||
			currentSnapshot.state === 'cancelled' ||
			currentSnapshot.state === 'failed'
		);
	}

	function getSnapshotRequestedScope(
		currentSnapshot: LaserAutoCalibrationSnapshot | null
	): LaserAutoCalibrationRequest {
		if (currentSnapshot?.requested_scope !== undefined) {
			return currentSnapshot.requested_scope;
		}

		if (activeRequestedScope !== null) {
			return activeRequestedScope;
		}

		if (currentSnapshot?.requested_scope === undefined) {
			return defaultRequestedScope;
		}

		return defaultRequestedScope;
	}

	function getShelvesForSide(side: '' | 'front_module' | 'back_module') {
		if (storageGrid === null || side === '') {
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

		return shelves.find((shelf) => String(shelf.shelf_idx) === selectedShelf) ?? null;
	}

	function getCells() {
		const currentShelf = getSelectedShelf();

		if (currentShelf === null) {
			return [];
		}

		return currentShelf.cells;
	}

	function syncSelection(preferredShelf?: string, preferredCell?: string): void {
		const shelves = getShelvesForSide(selectedSide);

		if (shelves.length === 0) {
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		if (preferredShelf === undefined || preferredShelf === '') {
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		const matchingShelf = shelves.find((shelf) => String(shelf.shelf_idx) === preferredShelf);

		if (matchingShelf === undefined) {
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		selectedShelf = String(matchingShelf.shelf_idx);

		if (preferredCell === undefined || preferredCell === '') {
			selectedCell = '';
			return;
		}

		const matchingCell = matchingShelf.cells.find(
			(cell) => String(cell.cell_idx) === preferredCell
		);

		if (matchingCell === undefined) {
			selectedCell = '';
			return;
		}

		selectedCell = String(matchingCell.cell_idx);
	}

	function syncScopeSelection(requestedScope: LaserAutoCalibrationRequest): void {
		isAngleCalibrationSelected = requestedScope.include_angle_calibration !== false;
		isExtensionCalibrationSelected = requestedScope.include_extension_calibration !== false;

		// Shelf and cell scopes always drop both options, so only a scope that honours them
		// may replace the remembered user choice.
		if (requestedScope.mode !== 'shelf' && requestedScope.mode !== 'cell') {
			preferredAngleCalibration = isAngleCalibrationSelected;
			preferredExtensionCalibration = isExtensionCalibrationSelected;
		}

		if (requestedScope.mode === 'all') {
			isAllSelected = true;
			selectedSide = '';
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		isAllSelected = false;

		if (requestedScope.mode === 'both') {
			selectedSide = '';
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		if (requestedScope.side !== undefined) {
			selectedSide = requestedScope.side;
		}

		const nextShelf =
			requestedScope.shelf_idx === undefined ? '' : String(requestedScope.shelf_idx);
		const nextCell = requestedScope.cell_idx === undefined ? '' : String(requestedScope.cell_idx);

		syncSelection(nextShelf, nextCell);

		if (requestedScope.mode === 'shelf' || requestedScope.mode === 'cell') {
			isAngleCalibrationSelected = false;
			isExtensionCalibrationSelected = false;
		}
	}

	/**
	 * Angle and extension calibration only apply to all-, both- and side-scoped runs, so a
	 * shelf or cell selection forces both off and remembering the last user choice keeps a
	 * later scope change from silently re-enabling what was just unchecked.
	 */
	function applyCalibrationOptionSelection(): void {
		if (selectedShelf !== '' || selectedCell !== '') {
			isAngleCalibrationSelected = false;
			isExtensionCalibrationSelected = false;
			return;
		}

		isAngleCalibrationSelected = preferredAngleCalibration;
		isExtensionCalibrationSelected = preferredExtensionCalibration;
	}

	function getRequestedScope(): LaserAutoCalibrationRequest {
		if (isAllSelected === true) {
			return {
				mode: 'all',
				include_angle_calibration: isAngleCalibrationSelected,
				include_extension_calibration: isExtensionCalibrationSelected
			};
		}

		if (selectedSide === '') {
			return {
				mode: 'both',
				include_angle_calibration: isAngleCalibrationSelected,
				include_extension_calibration: isExtensionCalibrationSelected
			};
		}

		if (selectedShelf === '') {
			return {
				mode: 'side',
				side: selectedSide,
				include_angle_calibration: isAngleCalibrationSelected,
				include_extension_calibration: isExtensionCalibrationSelected
			};
		}

		if (selectedCell === '') {
			return {
				mode: 'shelf',
				side: selectedSide,
				shelf_idx: Number(selectedShelf),
				include_angle_calibration: false,
				include_extension_calibration: false
			};
		}

		return {
			mode: 'cell',
			side: selectedSide,
			shelf_idx: Number(selectedShelf),
			cell_idx: Number(selectedCell),
			include_angle_calibration: false,
			include_extension_calibration: false
		};
	}

	function didCloseStream(): void {
		if (stream === null) {
			return;
		}

		stream.close();
		stream = null;
		isStreamOpen = false;
		isConnecting = false;
	}

	function didOpenStream(): void {
		isConnecting = false;
		isStreamOpen = true;
		error = '';
		message = 'auto calibration started';
		isStreamSubscribeOnly = true;
	}

	function didReceiveSnapshot(event: Event): void {
		if (event instanceof MessageEvent === false) {
			return;
		}

		try {
			snapshot = JSON.parse(event.data) as LaserAutoCalibrationSnapshot;

			if (snapshot !== null) {
				if (snapshot.requested_scope !== undefined) {
					activeRequestedScope = snapshot.requested_scope;
				}

				syncScopeSelection(getSnapshotRequestedScope(snapshot));
			}

			error = '';
			isStreamSubscribeOnly = true;

			if (snapshot.state === 'completed') {
				message = 'laser auto calibration completed';
				didCloseStream();
				return;
			}

			if (snapshot.state === 'cancelled') {
				message = 'laser auto calibration cancelled';
				didCloseStream();
				return;
			}

			if (snapshot.state === 'failed') {
				message = '';
				error = snapshot.error ?? 'laser auto calibration failed';
				didCloseStream();
			}
		} catch {
			error = 'failed to parse laser auto calibration event';
		}
	}

	async function didReceiveStreamError(): Promise<void> {
		isConnecting = false;
		isStreamOpen = false;

		if (isTerminalSnapshot(snapshot) === true && isStreamSubscribeOnly === true) {
			didCloseStream();
			return;
		}

		if (snapshot === null || shouldAutoConnect(snapshot) === false) {
			const request = activeRequestedScope ?? getRequestedScope();
			const shouldSubscribeOnly = isStreamSubscribeOnly;

			didCloseStream();

			const streamError = await getLaserAutoCalibrationStreamError(request, {
				subscribeOnly: shouldSubscribeOnly
			});
			error = streamError?.message ?? 'laser auto calibration stream disconnected';
			message = '';
			return;
		}

		error = '';
		message = 'laser auto calibration stream reconnecting';
	}

	function didChangeAll(event: Event): void {
		const target = event.target as HTMLInputElement;

		isAllSelected = target.checked;

		if (isAllSelected === true) {
			selectedSide = '';
			selectedShelf = '';
			selectedCell = '';
		}

		applyCalibrationOptionSelection();

		error = '';
		message = '';
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as '' | 'front_module' | 'back_module';
		syncSelection();
		applyCalibrationOptionSelection();
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		syncSelection(target.value);
		applyCalibrationOptionSelection();
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = target.value;
		applyCalibrationOptionSelection();
		error = '';
		message = '';
	}

	function didChangeAngleCalibration(event: Event): void {
		const target = event.target as HTMLInputElement;

		isAngleCalibrationSelected = target.checked;
		preferredAngleCalibration = target.checked;
		error = '';
		message = '';
	}

	function didChangeExtensionCalibration(event: Event): void {
		const target = event.target as HTMLInputElement;

		isExtensionCalibrationSelected = target.checked;
		preferredExtensionCalibration = target.checked;
		error = '';
		message = '';
	}

	async function didClickStart(): Promise<void> {
		if (browser === false) {
			return;
		}

		activeRequestedScope = getRequestedScope();

		didConnectStream(false);
	}

	function didConnectStream(shouldSubscribeOnly: boolean): void {
		didCloseStream();
		isConnecting = true;
		error = '';
		message = '';
		isStreamSubscribeOnly = shouldSubscribeOnly;

		const nextStream = createReconnectingEventSource({
			path: () =>
				buildLaserAutoCalibrationStreamUrl(activeRequestedScope ?? getRequestedScope(), {
					subscribeOnly: isStreamSubscribeOnly
				}),
			onOpen: didOpenStream,
			onError: didReceiveStreamError,
			events: {
				snapshot: didReceiveSnapshot
			}
		});
		stream = nextStream;
		nextStream.connect();
	}

	async function didClickAbort(): Promise<void> {
		isAborting = true;
		error = '';
		message = 'abort requested';

		if (snapshot !== null) {
			snapshot = {
				...snapshot,
				current_stage: 'cancelling'
			};
		}

		try {
			snapshot = await abortLaserAutoCalibration();

			if (snapshot !== null) {
				if (snapshot.requested_scope !== undefined) {
					activeRequestedScope = snapshot.requested_scope;
				}

				syncScopeSelection(getSnapshotRequestedScope(snapshot));
			}
		} catch (err) {
			message = '';
			error = err instanceof Error ? err.message : 'failed to abort auto calibration';
		} finally {
			isAborting = false;
		}
	}

	function formatNumber(value: number | null | undefined, digits = 2): string {
		if (value === null || value === undefined) {
			return '-';
		}

		return value.toFixed(digits);
	}

	function getAngleResults(): LaserAutoCalibrationAngleResult[] {
		return snapshot?.angle_results ?? [];
	}

	function getRotationValue(side: 'front_module' | 'back_module'): string {
		const angleResult = getAngleResults().find(function (result) {
			return result.side === side;
		});

		if (angleResult === undefined) {
			return '-';
		}

		return formatNumber(angleResult.updated_rotation_deg, 4);
	}

	function getStageExtensionValue(side: 'front_module' | 'back_module'): string {
		if (side === 'front_module') {
			return formatNumber(snapshot?.front_stage_extension_mm);
		}

		return formatNumber(snapshot?.back_stage_extension_mm);
	}

	function formatStageDetailValue(value: unknown): string {
		if (value === null || value === undefined) {
			return '-';
		}

		if (typeof value === 'number') {
			return formatNumber(value);
		}

		if (typeof value === 'string' || typeof value === 'boolean') {
			return String(value);
		}

		return JSON.stringify(value);
	}

	function getStageDetailRows(): { key: string; value: string }[] {
		if (snapshot === null) {
			return [];
		}

		return Object.entries(snapshot.stage_detail).map(([key, value]) => ({
			key,
			value: formatStageDetailValue(value)
		}));
	}
</script>

{#if isLoading === true}
	<p>loading auto calibration status...</p>
{:else}
	<table>
		<thead>
			<tr>
				<th colspan="3">scan selection</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td colspan="3">
					<label class="all-toggle">
						<input
							type="checkbox"
							checked={isAllSelected}
							onchange={didChangeAll}
							disabled={isSelectionDisabled}
						/>
						<span>calibrate all</span>
					</label>
				</td>
			</tr>
			<tr>
				<td>
					<div class="selection-field">
						<span class="field-label">side</span>
						<select
							value={selectedSide}
							onchange={didChangeSide}
							disabled={isAllSelected === true || isSelectionDisabled || storageGrid === null}
						>
							<option value="">-</option>
							<option value="front_module">front module</option>
							<option value="back_module">back module</option>
						</select>
					</div>
				</td>
				<td>
					<div class="selection-field">
						<span class="field-label">shelf</span>
						<select
							value={selectedShelf}
							onchange={didChangeShelf}
							disabled={isAllSelected === true ||
								isSelectionDisabled ||
								storageGrid === null ||
								selectedSide === ''}
						>
							<option value="">-</option>
							{#each getShelves() as shelf}
								<option value={String(shelf.shelf_idx)}>{shelf.shelf_idx}</option>
							{/each}
						</select>
					</div>
				</td>
				<td>
					<div class="selection-field">
						<span class="field-label">cell</span>
						<select
							value={selectedCell}
							onchange={didChangeCell}
							disabled={isAllSelected === true ||
								isSelectionDisabled ||
								storageGrid === null ||
								selectedShelf === ''}
						>
							<option value="">-</option>
							{#each getCells() as cell}
								<option value={String(cell.cell_idx)}>{cell.cell_idx}</option>
							{/each}
						</select>
					</div>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<label class="all-toggle">
						<input
							type="checkbox"
							checked={isAngleCalibrationSelected}
							onchange={didChangeAngleCalibration}
							disabled={isAngleCalibrationDisabled}
						/>
						<span>angle calibration</span>
					</label>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<label class="all-toggle">
						<input
							type="checkbox"
							checked={isExtensionCalibrationSelected}
							onchange={didChangeExtensionCalibration}
							disabled={isExtensionCalibrationDisabled}
						/>
						<span>extension calibration</span>
					</label>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="actions">
						<button type="button" onclick={didClickStart} disabled={isStartDisabled}>
							{startButtonLabel}
						</button>
						<button
							type="button"
							onclick={didClickAbort}
							disabled={isAborting === true || isSessionActive === false}
						>
							abort
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>

	{#if snapshot && snapshot.session_id !== null}
		<table style="margin-top: 1.5em">
			<thead>
				<tr>
					<th>auto results</th>
					<th>front / x</th>
					<th>back / y</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>state</td>
					<td colspan="2">{snapshot?.state ?? 'idle'}</td>
				</tr>
				<tr>
					<td>requested scope</td>
					<td colspan="2">{getSnapshotRequestedScope(snapshot).mode}</td>
				</tr>
				<tr>
					<td>current stage</td>
					<td colspan="2">{snapshot?.current_stage ?? '-'}</td>
				</tr>
				<tr>
					<td>scouted shelves</td>
					<td>{snapshot?.front_scouted_shelf_count ?? 0}</td>
					<td>{snapshot?.back_scouted_shelf_count ?? 0}</td>
				</tr>
				<tr>
					<td>candidate cells</td>
					<td>{snapshot?.front_candidate_count ?? 0}</td>
					<td>{snapshot?.back_candidate_count ?? 0}</td>
				</tr>
				<tr>
					<td>confirmed cells</td>
					<td>{snapshot?.front_detected_cell_count ?? 0}</td>
					<td>{snapshot?.back_detected_cell_count ?? 0}</td>
				</tr>
				<tr>
					<td>rotation angle</td>
					<td>{getRotationValue('front_module')}</td>
					<td>{getRotationValue('back_module')}</td>
				</tr>
				<tr>
					<td>stage extension</td>
					<td>{getStageExtensionValue('front_module')}</td>
					<td>{getStageExtensionValue('back_module')}</td>
				</tr>
			</tbody>
		</table>
	{/if}

	{#if snapshot && snapshot.session_id !== null}
		<table style="margin-top: 1.5em">
			<thead>
				<tr>
					<th colspan="2">stage detail</th>
				</tr>
			</thead>
			<tbody>
				{#each getStageDetailRows() as row}
					<tr>
						<td>{row.key}</td>
						<td>{row.value}</td>
					</tr>
				{:else}
					<tr>
						<td colspan="2">-</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	.actions {
		display: flex;
		gap: 1rem;
	}

	.all-toggle {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.field-label {
		display: inline-block;
	}

	.selection-field {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
</style>
