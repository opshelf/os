<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		calibrateGripperOpenPosition,
		getLaserAutoCalibrationStatus,
		getLaserAutoCalibrationStreamError,
		getLaserZStallEnabled,
		getStorageGrid,
		saveLaserZStallEnabled,
		type GripperOpenPositionResult,
		type LaserAutoCalibrationRequest,
		type LaserAutoCalibrationSnapshot,
		type StorageGrid
	} from '$lib/api';
	import { formatLaserFailure, normalizeLaserAutoCalibrationSnapshot } from '$lib/api';
	import { activeVocabulary, activeVocabularyStore, sideIndex, sideLabel } from '$lib/side';
	import {
		createReconnectingEventSource,
		refreshStreamAuthentication,
		type ReconnectingEventSource
	} from '$lib/sse';
	import { postToast } from '$lib/toasts';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let vocabulary = $state(activeVocabulary());
	$effect(() => activeVocabularyStore.subscribe((v) => {
		vocabulary = v;
	}));

	let snapshot = $state<LaserAutoCalibrationSnapshot | null>(null);
	let storageGrid = $state<StorageGrid | null>(null);
	let error = $state<string>('');
	let message = $state<string>('');
	let isLoading = $state<boolean>(true);
	let isConnecting = $state<boolean>(false);
	let isStreamOpen = $state<boolean>(false);
	let isAborting = $state<boolean>(false);
	let isAllSelected = $state<boolean>(true);
	let isGripperCalibrationSelected = $state<boolean>(false);
	let isMeasuringGripper = $state<boolean>(false);
	// A measure-now result is newer than the run snapshot's until the next run starts.
	let measuredGripperResult = $state<GripperOpenPositionResult | null>(null);
	let isZStallEnabled = $state<boolean>(false);
	let isSavingZStall = $state<boolean>(false);
	let selectedSide = $state<'' | 'front_module' | 'back_module'>('');
	let selectedShelf = $state<string>('');
	let selectedCell = $state<string>('');
	let activeRequestedScope = $state<LaserAutoCalibrationRequest | null>(null);
	let stream = $state<ReconnectingEventSource | null>(null);

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

	const isGripperCalibrationDisabled = $derived.by(() => {
		return selectedShelf !== '' || isSelectionDisabled === true;
	});

	const gripperResult = $derived(measuredGripperResult ?? snapshot?.gripper_open_position ?? null);

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
			const [nextSnapshot, nextStorageGrid, nextZStallEnabled] = await Promise.all([
				getLaserAutoCalibrationStatus(),
				getStorageGrid(),
				getLaserZStallEnabled()
			]);

			snapshot = nextSnapshot;
			isZStallEnabled = nextZStallEnabled;

			if (nextSnapshot.requested_scope !== undefined) {
				activeRequestedScope = nextSnapshot.requested_scope;
			}

			storageGrid = nextStorageGrid;
			syncScopeSelection(getSnapshotRequestedScope(nextSnapshot));

			if (shouldAutoConnect(nextSnapshot) === true) {
				didConnectStream();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load auto calibration status.';
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
		isGripperCalibrationSelected = requestedScope.include_gripper_calibration === true;

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

		if (requestedScope.side !== undefined && sideIndex(requestedScope.side) !== null) {
			selectedSide = sideIndex(requestedScope.side) === 1 ? 'back_module' : 'front_module';
		}

		const nextShelf =
			requestedScope.shelf_idx === undefined ? '' : String(requestedScope.shelf_idx);
		const nextCell = requestedScope.cell_idx === undefined ? '' : String(requestedScope.cell_idx);

		syncSelection(nextShelf, nextCell);

		if (requestedScope.mode === 'shelf' || requestedScope.mode === 'cell') {
			isGripperCalibrationSelected = false;
		}
	}

	// The gripper open position is only measured by all-, both- and side-scoped runs.
	function clearGripperForShelfOrCellScope(): void {
		if (selectedShelf !== '' || selectedCell !== '') {
			isGripperCalibrationSelected = false;
		}
	}

	function getRequestedScope(): LaserAutoCalibrationRequest {
		if (isAllSelected === true) {
			return {
				mode: 'all',
				include_gripper_calibration: isGripperCalibrationSelected
			};
		}

		if (selectedSide === '') {
			return {
				mode: 'both',
				include_gripper_calibration: isGripperCalibrationSelected
			};
		}

		if (selectedShelf === '') {
			return {
				mode: 'side',
				side: selectedSide,
				include_gripper_calibration: isGripperCalibrationSelected
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

		stream.close();
		stream = null;
		isStreamOpen = false;
		isConnecting = false;
	}

	function didOpenStream(): void {
		isConnecting = false;
		isStreamOpen = true;
		error = '';
		message = 'Auto calibration started.';
	}

	function didReceiveSnapshot(event: Event): void {
		if (event instanceof MessageEvent === false) {
			return;
		}

		try {
			snapshot = normalizeLaserAutoCalibrationSnapshot(JSON.parse(event.data));

			if (snapshot !== null) {
				if (snapshot.requested_scope !== undefined) {
					activeRequestedScope = snapshot.requested_scope;
				}

				syncScopeSelection(getSnapshotRequestedScope(snapshot));
			}

			error = '';

			if (snapshot.state === 'completed') {
				message = 'Laser auto calibration completed.';
				didCloseStream();
				return;
			}

			if (snapshot.state === 'cancelled') {
				message = 'Laser auto calibration cancelled.';
				didCloseStream();
				return;
			}

			if (snapshot.state === 'failed') {
				message = '';
				error = formatLaserFailure(snapshot);
				didCloseStream();
			}
		} catch {
			error = 'Failed to parse laser auto calibration event.';
		}
	}

	async function didReceiveStreamError(): Promise<void> {
		isConnecting = false;
		isStreamOpen = false;

		if (isTerminalSnapshot(snapshot) === true) {
			didCloseStream();
			return;
		}

		if (snapshot === null || shouldAutoConnect(snapshot) === false) {
			const request = activeRequestedScope ?? getRequestedScope();

			didCloseStream();

			const streamError = await getLaserAutoCalibrationStreamError(request, {
				subscribeOnly: true
			});
			error = streamError?.message ?? 'Laser auto calibration stream disconnected.';
			message = '';
			return;
		}

		error = '';
		message = 'Laser auto calibration stream reconnecting.';
	}

	function didChangeAll(event: Event): void {
		const target = event.target as HTMLInputElement;

		isAllSelected = target.checked;

		if (isAllSelected === true) {
			selectedSide = '';
			selectedShelf = '';
			selectedCell = '';
		}

		clearGripperForShelfOrCellScope();

		error = '';
		message = '';
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as '' | 'front_module' | 'back_module';
		syncSelection();
		clearGripperForShelfOrCellScope();
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		syncSelection(target.value);
		clearGripperForShelfOrCellScope();
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = target.value;
		clearGripperForShelfOrCellScope();
		error = '';
		message = '';
	}

	function didChangeGripperCalibration(event: Event): void {
		const target = event.target as HTMLInputElement;

		isGripperCalibrationSelected = target.checked;
		error = '';
		message = '';
	}

	async function didClickMeasureGripper(): Promise<void> {
		isMeasuringGripper = true;
		error = '';
		message = '';

		try {
			const result = await calibrateGripperOpenPosition(selectedSide === '' ? undefined : selectedSide);
			measuredGripperResult = result;
			message = formatGripperResult(result);
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'Failed to measure the gripper open position.');
		} finally {
			isMeasuringGripper = false;
		}
	}

	function formatGripperResult(result: GripperOpenPositionResult | null | undefined): string {
		if (result === null || result === undefined) {
			return '-';
		}

		const stops = result.samples
			.map((sample) => `${sideLabel(sideIndex(sample.side) ?? 0, vocabulary).toLowerCase()} stop ${sample.stop_position ?? '-'}`)
			.join(', ');
		const target = result.open_position === null ? 'not measured' : String(result.open_position);
		const outcome = result.applied ? 'saved' : 'kept';
		const skipped = result.skipped.length > 0 ? ` (${result.skipped.join('; ')})` : '';

		return `open position ${result.previous_open_position} -> ${target}, ${outcome}; ${stops}${skipped}`;
	}

	async function didChangeZStall(event: Event): Promise<void> {
		const target = event.target as HTMLInputElement;
		const requested = target.checked;

		isSavingZStall = true;

		try {
			isZStallEnabled = await saveLaserZStallEnabled(requested);
		} catch (err) {
			target.checked = isZStallEnabled;
			postToast('error', err instanceof Error ? err.message : 'Failed to save the grip edge setting.');
		} finally {
			isSavingZStall = false;
		}
	}

	async function didClickStart(): Promise<void> {
		if (browser === false) {
			return;
		}

		activeRequestedScope = getRequestedScope();
		measuredGripperResult = null;

		didCloseStream();
		isConnecting = true;
		error = '';
		message = '';

		// The only request that may start motion. The stream, its reconnects and its error
		// probes all subscribe only, so a dropped connection can never start a second run.
		await refreshStreamAuthentication();
		const startError = await getLaserAutoCalibrationStreamError(activeRequestedScope, {
			reportNetworkFailure: true
		});

		if (startError !== null) {
			// Status 0 is a lost response, not a refusal: the server may have started the run
			// before the connection dropped. Attach (subscribe only) to a live run of this scope
			// so it keeps its stream and Abort; never send a second start-capable request.
			let startedSnapshot: LaserAutoCalibrationSnapshot | null = null;

			if (startError.status === 0) {
				try {
					const status = await getLaserAutoCalibrationStatus();

					if (
						shouldAutoConnect(status) === true &&
						buildLaserAutoCalibrationStreamUrl(getSnapshotRequestedScope(status)) ===
							buildLaserAutoCalibrationStreamUrl(activeRequestedScope)
					) {
						startedSnapshot = status;
					}
				} catch {
					// Show the start error below.
				}
			}

			if (startedSnapshot === null) {
				isConnecting = false;
				error = startError.message;
				return;
			}

			snapshot = startedSnapshot;
			didConnectStream();
			return;
		}

		// Seed the running state so a stream error before the first event reconnects
		// instead of dropping a run that is already moving the robot.
		try {
			snapshot = await getLaserAutoCalibrationStatus();
		} catch {
			// The stream's first event carries the same snapshot.
		}

		didConnectStream();
	}

	function didConnectStream(): void {
		didCloseStream();
		isConnecting = true;
		error = '';
		message = '';

		const nextStream = createReconnectingEventSource({
			path: () =>
				buildLaserAutoCalibrationStreamUrl(activeRequestedScope ?? getRequestedScope(), {
					subscribeOnly: true
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
		message = 'Abort requested.';

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
			error = err instanceof Error ? err.message : 'Failed to abort auto calibration.';
		} finally {
			isAborting = false;
		}
	}
</script>

{#if isLoading === true}
	<p>loading auto calibration status...</p>
{:else}
	<div class="scroll-x">
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
								<option value="front_module">{sideLabel(0, vocabulary).toLowerCase()} module</option>
								<option value="back_module">{sideLabel(1, vocabulary).toLowerCase()} module</option>
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
						<div class="actions">
							<label class="all-toggle">
								<input
									type="checkbox"
									checked={isGripperCalibrationSelected}
									onchange={didChangeGripperCalibration}
									disabled={isGripperCalibrationDisabled}
								/>
								<span>gripper open position</span>
							</label>
							<button
								type="button"
								onclick={didClickMeasureGripper}
								disabled={isMeasuringGripper === true || isSelectionDisabled === true}
							>
								measure now
							</button>
						</div>
					</td>
				</tr>
				<tr>
					<td colspan="3">
						<label class="all-toggle">
							<input
								type="checkbox"
								checked={isZStallEnabled}
								onchange={didChangeZStall}
								disabled={isSavingZStall === true || isSelectionDisabled === true}
							/>
							<span>grip edge z</span>
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
	</div>

	{#if snapshot && snapshot.session_id !== null}
		<div class="scroll-x result-table">
			<table>
				<thead>
					<tr>
						<th>auto results</th>
						<th>{sideLabel(0, vocabulary).toLowerCase()} / x</th>
						<th>{sideLabel(1, vocabulary).toLowerCase()} / y</th>
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
					{#if gripperResult !== null}
						<tr>
							<td>gripper open position</td>
							<td colspan="2">{formatGripperResult(gripperResult)}</td>
						</tr>
					{/if}
				</tbody>
			</table>
		</div>
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	.result-table {
		margin-top: 1.5em;
	}

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
