<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		getLaserAutoCalibrationStatus,
		type LaserAutoCalibrationAngleResult,
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

	const startButtonLabel = 'calibrate';

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
		message = 'auto calibration started';
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

	<table>
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
		margin-bottom: 1rem;
	}

	pre {
		margin: 0;
		overflow: auto;
	}

	table {
		margin-top: 1em;
	}

</style>
