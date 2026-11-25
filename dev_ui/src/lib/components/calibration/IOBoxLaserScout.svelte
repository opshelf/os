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
					<th>parameter</th>
					<th>current</th>
					<th>proposed</th>
					<th>measured from</th>
				</tr>
				<tr>
					<td>low / multi-item x</td>
					<td>{mm(result.manual.pickup_low?.[0])} / {mm(result.manual.pickup_multi_item?.[0])}</td>
					<td>{mm(result.proposed_x_mm)}</td>
					<td>opening edge x {mm(result.opening_right_edge_tool_x_mm)}</td>
				</tr>
				<tr>
					<td>low y</td>
					<td>{mm(result.manual.pickup_low?.[1])}</td>
					<td>{mm(result.proposed_low_y_mm)}</td>
					<td rowspan="2">plate bottom y {mm(result.plate_bottom_tool_y_mm)}</td>
				</tr>
				<tr>
					<td>multi-item y</td>
					<td>{mm(result.manual.pickup_multi_item?.[1])}</td>
					<td>{mm(result.proposed_multi_item_y_mm)}</td>
				</tr>
				<tr>
					<td>z io</td>
					<td>{mm(result.manual.z_extension_io)}</td>
					<td>{mm(result.proposed_z_io_mm)}</td>
					<td rowspan="2">face depth {mm(result.plate_face_depth_mm)} as z {mm(result.face_equivalent_z_mm)}</td>
				</tr>
				<tr>
					<td>z multi-item</td>
					<td>{mm(result.manual.z_extension_io_multi_item)}</td>
					<td>{mm(result.proposed_z_multi_item_mm)}</td>
				</tr>
				<tr>
					<td>stage io / multi-item</td>
					<td>{mm(result.manual.stage_extension_io_box)} / {mm(result.manual.stage_extension_io_box_multi_item)}</td>
					<td>{mm(result.proposed_stage_extension_mm)}</td>
					<td>fixed</td>
				</tr>
				<tr>
					<td>high y</td>
					<td>{mm(result.manual.pickup_high?.[1])}</td>
					<td>-</td>
					<td>not derived</td>
				</tr>
				<tr>
					<td colspan="4">
						plate top y {mm(result.plate_top_tool_y_mm)}, thickness {mm(result.plate_thickness_mm)}{result.plate_thickness_matches === false ? ' (not the 8mm plate)' : ''}, plate right edge x {mm(result.plate_right_edge_tool_x_mm)}
					</td>
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
