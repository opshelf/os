<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		abortLaserAutoCalibration,
		buildLaserAutoCalibrationStreamUrl,
		formatLaserFailure,
		getLaserAutoCalibrationStatus,
		getLaserAutoCalibrationStreamError,
		normalizeLaserAutoCalibrationSnapshot,
		type IOBoxScoutProfileSegment,
		type LaserAutoCalibrationRequest,
		type LaserAutoCalibrationSnapshot
	} from '$lib/api';
	import {
		createReconnectingEventSource,
		refreshStreamAuthentication,
		type ReconnectingEventSource
	} from '$lib/sse';
	import { postToast } from '$lib/toasts';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	const request: LaserAutoCalibrationRequest = { mode: 'io_box' };

	let snapshot = $state<LaserAutoCalibrationSnapshot | null>(null);
	let stream = $state<ReconnectingEventSource | null>(null);
	let isStarting = $state(false);
	let isAborting = $state(false);
	let message = $state('');

	const isScoutSnapshot = $derived(snapshot?.requested_scope?.mode === 'io_box');
	const isRunning = $derived(
		snapshot !== null &&
			(snapshot.state === 'starting' || snapshot.state === 'running' || snapshot.state === 'applying')
	);
	const result = $derived(isScoutSnapshot === true ? (snapshot?.io_box_scout ?? null) : null);

	function mm(value: number | null | undefined): string {
		return value === null || value === undefined ? '-' : value.toFixed(1);
	}

	function profileText(segments: IOBoxScoutProfileSegment[]): string {
		return segments
			.map(
				(segment) =>
					`${segment.start_mm.toFixed(1)}..${segment.end_mm.toFixed(1)}: ${
						segment.depth_mm === null ? 'none' : segment.depth_mm.toFixed(1)
					}`
			)
			.join('\n');
	}

	function closeStream(): void {
		stream?.close();
		stream = null;
	}

	function didReceiveSnapshot(event: Event): void {
		snapshot = normalizeLaserAutoCalibrationSnapshot(JSON.parse((event as MessageEvent<string>).data));

		if (snapshot.state === 'completed') {
			message = 'IO box scout completed.';
			closeStream();
		} else if (snapshot.state === 'cancelled') {
			message = 'IO box scout cancelled.';
			closeStream();
		} else if (snapshot.state === 'failed') {
			message = '';
			postToast('error', formatLaserFailure(snapshot));
			closeStream();
		}
	}

	function connectStream(): void {
		closeStream();
		const nextStream = createReconnectingEventSource({
			path: () => buildLaserAutoCalibrationStreamUrl(request, { subscribeOnly: true }),
			events: { snapshot: didReceiveSnapshot }
		});
		stream = nextStream;
		nextStream.connect();
	}

	async function didClickStart(): Promise<void> {
		isStarting = true;
		message = '';

		try {
			// The only request that may start motion; the stream itself only subscribes.
			await refreshStreamAuthentication();
			const startError = await getLaserAutoCalibrationStreamError(request, { reportNetworkFailure: true });

			if (startError !== null) {
				postToast('error', startError.message);
				return;
			}

			snapshot = await getLaserAutoCalibrationStatus();
			connectStream();
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'The IO box scout did not start.');
		} finally {
			isStarting = false;
		}
	}

	async function didClickAbort(): Promise<void> {
		isAborting = true;

		try {
			snapshot = await abortLaserAutoCalibration();
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'The IO box scout did not abort.');
		} finally {
			isAborting = false;
		}
	}

	onMount(async () => {
		try {
			snapshot = await getLaserAutoCalibrationStatus();
		} catch {
			return;
		}

		if (isScoutSnapshot === true && isRunning === true) {
			connectStream();
		}
	});

	onDestroy(closeStream);
</script>

<div class="section-divider" style="margin: 1.5em 0;"></div>

<div class="scroll-x">
	<table>
		<thead>
			<tr>
				<th colspan="4">io box laser scout (measure only, nothing is saved)</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td colspan="4">
					<div class="scout-actions">
						<button
							type="button"
							onclick={didClickStart}
							disabled={isStarting === true || isRunning === true}
						>
							run scout
						</button>
						<button
							type="button"
							onclick={didClickAbort}
							disabled={isScoutSnapshot === false || isRunning === false || isAborting === true}
						>
							abort
						</button>
						{#if isScoutSnapshot === true && snapshot !== null}
							<span>{snapshot.state} / {snapshot.current_stage ?? '-'}</span>
						{/if}
					</div>
				</td>
			</tr>
			{#if result !== null}
				<tr>
					<th>measured</th>
					<th>tool frame</th>
					<th>manual</th>
					<th>manual - measured</th>
				</tr>
				<tr>
					<td>plate right edge x</td>
					<td>{mm(result.plate_right_edge_tool_x_mm)}</td>
					<td>multi-item x {mm(result.manual.pickup_multi_item?.[0])}</td>
					<td>{mm(result.multi_item_x_minus_right_edge_mm)}</td>
				</tr>
				<tr>
					<td>plate top y</td>
					<td>{mm(result.plate_top_tool_y_mm)}</td>
					<td>multi-item y {mm(result.manual.pickup_multi_item?.[1])}</td>
					<td>{mm(result.multi_item_y_minus_plate_top_mm)}</td>
				</tr>
				<tr>
					<td>plate top y</td>
					<td>{mm(result.plate_top_tool_y_mm)}</td>
					<td>low y {mm(result.manual.pickup_low?.[1])}, high y {mm(result.manual.pickup_high?.[1])}</td>
					<td>{mm(result.low_y_minus_plate_top_mm)}, {mm(result.high_y_minus_plate_top_mm)}</td>
				</tr>
				<tr>
					<td>plate bottom y / thickness</td>
					<td>{mm(result.plate_bottom_tool_y_mm)} / {mm(result.plate_thickness_mm)}</td>
					<td colspan="2">{result.plate_thickness_matches === false ? 'not the 8mm plate' : ''}</td>
				</tr>
				<tr>
					<td>face depth {mm(result.plate_face_depth_mm)} as z (multi-item stage)</td>
					<td>{mm(result.face_equivalent_z_multi_item_mm)}</td>
					<td>z multi-item {mm(result.manual.z_extension_io_multi_item)}</td>
					<td>{mm(result.z_multi_item_minus_face_equivalent_mm)}</td>
				</tr>
				<tr>
					<td>face depth as z (io stage)</td>
					<td>{mm(result.face_equivalent_z_io_mm)}</td>
					<td>z io {mm(result.manual.z_extension_io)}</td>
					<td>{mm(result.z_io_minus_face_equivalent_mm)}</td>
				</tr>
				{#each result.notes as note}
					<tr>
						<td colspan="4">{note}</td>
					</tr>
				{/each}
				<tr>
					<td colspan="4">
						<details>
							<summary>laser profiles (laser frame, position: depth)</summary>
							<pre>vertical line
{profileText(result.y_profile)}

plate x line
{profileText(result.plate_x_profile)}

above plate x line
{profileText(result.above_plate_x_profile)}</pre>
						</details>
					</td>
				</tr>
			{/if}
		</tbody>
	</table>
</div>

{#if message !== ''}
	<StatusMessage {message} />
{/if}

<style>
	.scout-actions {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.section-divider {
		margin-top: 1.5em;
		border-top: 2px black dashed;
	}

	td button {
		margin-top: 0;
	}
</style>
