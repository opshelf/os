<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		getLaserAutoCalibrationStatus,
		getStorageGrid,
		type LaserAutoCalibrationAngleResult,
		type LaserAutoCalibrationRequest,
		type LaserAutoCalibrationSnapshot,
		type StorageGrid
	} from '$lib/api';
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
	let selectedSide = $state<'left_module' | 'right_module'>('left_module');
	let selectedShelf = $state<string>('');
	let selectedCell = $state<string>('');
	let stream = $state<EventSource | null>(null);

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

	const startButtonLabel = 'calibrate';
	const defaultRequestedScope: LaserAutoCalibrationRequest = { mode: 'all' };

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
			storageGrid = nextStorageGrid;
			syncScopeSelection(getSnapshotRequestedScope(nextSnapshot));

			if (shouldAutoConnect(nextSnapshot) === true) {
				void didClickStart();
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

	function getSnapshotRequestedScope(
		currentSnapshot: LaserAutoCalibrationSnapshot | null
	): LaserAutoCalibrationRequest {
		if (currentSnapshot?.requested_scope === undefined) {
			return defaultRequestedScope;
		}

		return currentSnapshot.requested_scope;
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
		if (requestedScope.mode === 'all') {
			isAllSelected = true;
			selectedShelf = '';
			selectedCell = '';
			return;
		}

		isAllSelected = false;

		if (requestedScope.side !== undefined) {
			selectedSide = requestedScope.side;
		}

		const nextShelf =
			requestedScope.shelf_idx === undefined ? '' : String(requestedScope.shelf_idx);
		const nextCell = requestedScope.cell_idx === undefined ? '' : String(requestedScope.cell_idx);

		syncSelection(nextShelf, nextCell);
	}

	function getRequestedScope(): LaserAutoCalibrationRequest {
		if (isAllSelected === true) {
			return { mode: 'all' };
		}

		if (selectedShelf === '') {
			return {
				mode: 'side',
				side: selectedSide
			};
		}

		if (selectedCell === '') {
			return {
				mode: 'shelf',
				side: selectedSide,
				shelf_idx: Number(selectedShelf)
			};
		}

		return {
			mode: 'cell',
			side: selectedSide,
			shelf_idx: Number(selectedShelf),
			cell_idx: Number(selectedCell)
		};
	}

	function didCloseStream(): void {
		if (stream === null) {
			return;
		}

		stream.removeEventListener('snapshot', didReceiveSnapshot);
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
	}

	function didReceiveSnapshot(event: Event): void {
		if (event instanceof MessageEvent === false) {
			return;
		}

		try {
			snapshot = JSON.parse(event.data) as LaserAutoCalibrationSnapshot;

			if (snapshot !== null) {
				syncScopeSelection(getSnapshotRequestedScope(snapshot));
			}

			error = '';

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

	function didReceiveStreamError(): void {
		isConnecting = false;
		isStreamOpen = false;

		if (snapshot === null) {
			error = 'laser auto calibration stream disconnected';
			return;
		}

		if (shouldAutoConnect(snapshot) === false) {
			error = 'laser auto calibration stream disconnected';
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
			selectedShelf = '';
			selectedCell = '';
		}

		error = '';
		message = '';
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'left_module' | 'right_module';
		syncSelection();
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		syncSelection(target.value);
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = target.value;
		error = '';
		message = '';
	}

	async function didClickStart(): Promise<void> {
		if (browser === false) {
			return;
		}

		didCloseStream();
		isConnecting = true;
		error = '';
		message = '';

		const nextStream = new EventSource(buildLaserAutoCalibrationStreamUrl(getRequestedScope()));
		nextStream.addEventListener('snapshot', didReceiveSnapshot);
		nextStream.onopen = didOpenStream;
		nextStream.onerror = didReceiveStreamError;
		stream = nextStream;
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

	function getRotationValue(side: 'left_module' | 'right_module'): string {
		const angleResult = getAngleResults().find(function (result) {
			return result.side === side;
		});

		if (angleResult === undefined) {
			return '-';
		}

		return formatNumber(angleResult.updated_rotation_deg, 4);
	}

	function getStageDetail(): string {
		if (snapshot === null) {
			return '{}';
		}

		return JSON.stringify(snapshot.stage_detail, null, 2);
	}
</script>

{#if isLoading === true}
	<p>loading auto calibration status...</p>
{:else}
	<table>
		<thead>
			<tr>
				<th colspan="4">scan selection</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td class="all-column">
					<label class="all-toggle">
						<input
							type="checkbox"
							checked={isAllSelected}
							onchange={didChangeAll}
							disabled={isSelectionDisabled}
						/>
						<span>all</span>
					</label>
				</td>
				<td>
					<div class="selection-field">
						<span class="field-label">side</span>
						<select
							value={selectedSide}
							onchange={didChangeSide}
							disabled={isAllSelected === true || isSelectionDisabled || storageGrid === null}
						>
							<option value="left_module">left module</option>
							<option value="right_module">right module</option>
						</select>
					</div>
				</td>
				<td>
					<div class="selection-field">
						<span class="field-label">shelf</span>
						<select
							value={selectedShelf}
							onchange={didChangeShelf}
							disabled={isAllSelected === true || isSelectionDisabled || storageGrid === null}
						>
							<option value="">-</option>
							{#each getShelves() as shelf}
								<option value={shelf.shelf_idx}>{shelf.shelf_idx}</option>
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
								<option value={cell.cell_idx}>{cell.cell_idx}</option>
							{/each}
						</select>
					</div>
				</td>
			</tr>
			<tr>
				<td colspan="4">
					<div class="actions">
						<button
							type="button"
							onclick={didClickStart}
							disabled={isConnecting === true ||
								isStreamOpen === true ||
								isStreamReconnecting === true}
						>
							{startButtonLabel}
						</button>
						<button
							type="button"
							onclick={didClickAbort}
							disabled={isAborting === true || isSessionActive === false}
						>
							{isAborting === true ? 'aborting...' : 'abort'}
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>

	<table style="margin-top: 1.5em">
		<thead>
			<tr>
				<th colspan="2">cell auto</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>state</td>
				<td>{snapshot?.state ?? 'idle'}</td>
			</tr>
			<tr>
				<td>requested scope</td>
				<td>{getSnapshotRequestedScope(snapshot).mode}</td>
			</tr>
			<tr>
				<td>current side</td>
				<td>{snapshot?.current_side ?? '-'}</td>
			</tr>
			<tr>
				<td>current stage</td>
				<td>{snapshot?.current_stage ?? '-'}</td>
			</tr>
			<tr>
				<td>scouted shelves left</td>
				<td>{snapshot?.left_scouted_shelf_count ?? 0}</td>
			</tr>
			<tr>
				<td>scouted shelves right</td>
				<td>{snapshot?.right_scouted_shelf_count ?? 0}</td>
			</tr>
			<tr>
				<td>candidates left</td>
				<td>{snapshot?.left_candidate_count ?? 0}</td>
			</tr>
			<tr>
				<td>candidates right</td>
				<td>{snapshot?.right_candidate_count ?? 0}</td>
			</tr>
			<tr>
				<td>detected cells left</td>
				<td>{snapshot?.left_detected_cell_count ?? 0}</td>
			</tr>
			<tr>
				<td>detected cells right</td>
				<td>{snapshot?.right_detected_cell_count ?? 0}</td>
			</tr>
			<tr>
				<td>rotation angle left</td>
				<td>{getRotationValue('left_module')}</td>
			</tr>
			<tr>
				<td>rotation angle right</td>
				<td>{getRotationValue('right_module')}</td>
			</tr>
		</tbody>
	</table>

	<table style="margin-top: 1.5em">
		<thead>
			<tr>
				<th>stage detail</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>
					<pre>{getStageDetail()}</pre>
				</td>
			</tr>
		</tbody>
	</table>

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

	.all-column {
		width: 6rem;
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

	pre {
		margin: 0;
		overflow: auto;
	}
</style>
