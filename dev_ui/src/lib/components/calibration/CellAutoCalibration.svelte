<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		getLaserAutoCalibrationStatus,
		type LaserAutoCalibrationAngleResult,
		type LaserAutoCalibrationCandidate,
		type LaserAutoCalibrationCell,
		type LaserAutoCalibrationShelfEdge,
		type LaserAutoCalibrationSnapshot
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let snapshot = $state<LaserAutoCalibrationSnapshot | null>(null);
	let error = $state<string>('');
	let message = $state<string>('');
	let isLoading = $state<boolean>(true);
	let isConnecting = $state<boolean>(false);
	let isStreamOpen = $state<boolean>(false);
	let isAborting = $state<boolean>(false);
	let stream = $state<EventSource | null>(null);

	const isSessionActive = $derived.by(() => {
		if (snapshot === null) {
			return false;
		}

		return (
			snapshot.state === 'starting' ||
			snapshot.state === 'running' ||
			snapshot.state === 'applying'
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

	const startButtonLabel = $derived.by(() => {
		if (isConnecting === true) {
			return 'connecting...';
		}

		if (isStreamReconnecting === true) {
			return 'reconnecting...';
		}

		if (isStreamOpen === true) {
			return 'stream connected';
		}

		if (snapshot !== null && shouldAutoConnect(snapshot) === true) {
			return 'reconnect stream';
		}

		return 'start auto calibration';
	});

	onMount(function () {
		if (browser === false) {
			return;
		}

		void didLoadStatus();

		return function () {
			didCloseStream();
		};
	});

	async function didLoadStatus(): Promise<void> {
		isLoading = true;
		error = '';

		try {
			const nextSnapshot = await getLaserAutoCalibrationStatus();
			snapshot = nextSnapshot;

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
		message = 'laser auto calibration stream connected';
	}

	function didReceiveSnapshot(event: Event): void {
		if ((event instanceof MessageEvent) === false) {
			return;
		}

		try {
			snapshot = JSON.parse(event.data) as LaserAutoCalibrationSnapshot;
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

	async function didClickStart(): Promise<void> {
		if (browser === false) {
			return;
		}

		didCloseStream();
		isConnecting = true;
		error = '';
		message = '';

		const nextStream = new EventSource(buildLaserAutoCalibrationStreamUrl());
		nextStream.addEventListener('snapshot', didReceiveSnapshot);
		nextStream.onopen = didOpenStream;
		nextStream.onerror = didReceiveStreamError;
		stream = nextStream;
	}

	async function didClickAbort(): Promise<void> {
		isAborting = true;
		error = '';
		message = '';

		try {
			snapshot = await abortLaserAutoCalibration();
			message = 'abort requested';
		} catch (err) {
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

	function formatBounds(): string {
		if (snapshot?.bounds === null || snapshot?.bounds === undefined) {
			return '-';
		}

		return `x=${formatNumber(snapshot.bounds.x_min)}..${formatNumber(snapshot.bounds.x_max)} y=${formatNumber(snapshot.bounds.y_min)}..${formatNumber(snapshot.bounds.y_max)}`;
	}

	function getShelfEdges(): LaserAutoCalibrationShelfEdge[] {
		return snapshot?.shelf_edges ?? [];
	}

	function getCandidates(): LaserAutoCalibrationCandidate[] {
		return snapshot?.shelf_candidates ?? [];
	}

	function getDetectedCells(): LaserAutoCalibrationCell[] {
		return snapshot?.detected_cells ?? [];
	}

	function getAngleResults(): LaserAutoCalibrationAngleResult[] {
		return snapshot?.angle_results ?? [];
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
	<div class="actions">
		<button
			type="button"
			onclick={didClickStart}
			disabled={isConnecting === true || isStreamOpen === true || isStreamReconnecting === true}
		>
			{startButtonLabel}
		</button>
		<button type="button" onclick={didClickAbort} disabled={isAborting === true || isSessionActive === false}>
			{isAborting === true ? 'aborting...' : 'abort'}
		</button>
	</div>

	<table>
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
				<td>current side</td>
				<td>{snapshot?.current_side ?? '-'}</td>
			</tr>
			<tr>
				<td>current stage</td>
				<td>{snapshot?.current_stage ?? '-'}</td>
			</tr>
			<tr>
				<td>session</td>
				<td>{snapshot?.session_id ?? '-'}</td>
			</tr>
			<tr>
				<td>run</td>
				<td>{snapshot?.run_id ?? '-'}</td>
			</tr>
			<tr>
				<td>bounds</td>
				<td>{formatBounds()}</td>
			</tr>
			<tr>
				<td>scouted shelves</td>
				<td>{snapshot?.scouted_shelf_count ?? 0}</td>
			</tr>
			<tr>
				<td>candidates</td>
				<td>{snapshot?.candidate_count ?? 0}</td>
			</tr>
			<tr>
				<td>detected cells</td>
				<td>{snapshot?.detected_cell_count ?? 0}</td>
			</tr>
		</tbody>
	</table>

	{#if getAngleResults().length > 0}
		<h3>angle results</h3>
		<table>
			<thead>
				<tr>
					<th>side</th>
					<th>alpha</th>
					<th>capture y</th>
					<th>updated rotation</th>
					<th>samples</th>
				</tr>
			</thead>
			<tbody>
				{#each getAngleResults() as result}
					<tr>
						<td>{result.side}</td>
						<td>{formatNumber(result.alpha_deg, 4)}</td>
						<td>{formatNumber(result.capture_y_mm)}</td>
						<td>{formatNumber(result.updated_rotation_deg, 4)}</td>
						<td>{result.sample_count}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if getShelfEdges().length > 0}
		<h3>shelf edges</h3>
		<table>
			<thead>
				<tr>
					<th>side</th>
					<th>x</th>
					<th>y</th>
				</tr>
			</thead>
			<tbody>
				{#each getShelfEdges() as edge}
					<tr>
						<td>{edge.side}</td>
						<td>{formatNumber(edge.x_mm)}</td>
						<td>{formatNumber(edge.y_mm)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if getCandidates().length > 0}
		<h3>candidates</h3>
		<table>
			<thead>
				<tr>
					<th>side</th>
					<th>shelf</th>
					<th>x min</th>
					<th>x max</th>
					<th>y min</th>
					<th>y max</th>
				</tr>
			</thead>
			<tbody>
				{#each getCandidates() as candidate}
					<tr>
						<td>{candidate.side}</td>
						<td>{candidate.shelf_idx ?? '-'}</td>
						<td>{formatNumber(candidate.x_min)}</td>
						<td>{formatNumber(candidate.x_max)}</td>
						<td>{formatNumber(candidate.y_min)}</td>
						<td>{formatNumber(candidate.y_max)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if getDetectedCells().length > 0}
		<h3>detected cells</h3>
		<table>
			<thead>
				<tr>
					<th>side</th>
					<th>shelf</th>
					<th>cell</th>
					<th>tool x</th>
					<th>tool y</th>
					<th>center x</th>
					<th>center y</th>
				</tr>
			</thead>
			<tbody>
				{#each getDetectedCells() as cell}
					<tr>
						<td>{cell.side}</td>
						<td>{cell.shelf_idx}</td>
						<td>{cell.cell_idx}</td>
						<td>{formatNumber(cell.tool_x_mm)}</td>
						<td>{formatNumber(cell.tool_y_mm)}</td>
						<td>{formatNumber(cell.center_x_mm)}</td>
						<td>{formatNumber(cell.center_y_mm)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<h3>stage detail</h3>
	<pre>{getStageDetail()}</pre>

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
		margin-bottom: 1rem;
	}

	pre {
		padding: 1rem;
		border: 1px solid #ccc;
		background: #f7f7f7;
		overflow: auto;
	}

	h3 {
		margin-top: 1.5rem;
	}
</style>
