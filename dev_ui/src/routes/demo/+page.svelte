<script lang="ts">
	import { browser } from '$app/environment';
	import { onDestroy, onMount } from 'svelte';
	import {
		apiFetch,
		buildRecordingDownloadUrl,
		deleteCameraRecording,
		getCameraRecordingStatus,
		listCameraRecordings,
		startCameraRecording,
		stopCameraRecording,
		type CameraRecordStatus,
		type CameraRecordingFile
	} from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	type CameraInfo = {
		id: string;
		type: string;
		online: boolean;
		running: boolean;
		consumers: number;
		has_frame: boolean;
	};

	type CamerasResponse = {
		cameras: CameraInfo[];
	};

	const POLL_INTERVAL_MS = 2000;

	let cameras: CameraInfo[] = [];
	let recordings: CameraRecordingFile[] = [];
	let pageError = '';
	let loading = true;
	const recordingStatus = new Map<string, CameraRecordStatus>();
	const pendingAction = new Set<string>();

	let pollTimer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		if (browser === false) {
			return;
		}

		void loadPageData();
		pollTimer = setInterval(() => {
			void refreshState();
		}, POLL_INTERVAL_MS);
	});

	onDestroy(() => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	});

	async function loadPageData(): Promise<void> {
		loading = true;
		await refreshState();
		loading = false;
	}

	async function refreshState(): Promise<void> {
		try {
			const response = await apiFetch<CamerasResponse>('/camera');
			cameras = response.cameras;
			pageError = '';
		} catch (error) {
			pageError = error instanceof Error ? error.message : 'unable to load cameras';
		}

		for (const camera of cameras) {
			try {
				recordingStatus.set(
					camera.id,
					await getCameraRecordingStatus(camera.id)
				);
			} catch {
				// A per-camera status failure must not blank the page.
			}
		}

		try {
			recordings = await listCameraRecordings();
		} catch (error) {
			pageError =
				pageError === ''
					? error instanceof Error
						? error.message
						: 'unable to list recordings'
					: pageError;
		}
	}

	async function didToggleRecord(camera: CameraInfo): Promise<void> {
		if (pendingAction.has(camera.id)) {
			return;
		}

		pendingAction.add(camera.id);

		try {
			if (recordingStatus.get(camera.id)?.is_recording === true) {
				await stopCameraRecording(camera.id);
			} else {
				await startCameraRecording(camera.id);
			}
			recordingStatus.set(
				camera.id,
				await getCameraRecordingStatus(camera.id)
			);
		} catch (error) {
			pageError = error instanceof Error ? error.message : 'record action failed';
		} finally {
			pendingAction.delete(camera.id);
		}
	}

	async function didDeleteRecording(file: CameraRecordingFile): Promise<void> {
		if (file.is_recording === true) {
			return;
		}

		if (confirm(`delete ${file.file_name}?`) === false) {
			return;
		}

		try {
			await deleteCameraRecording(file.camera_id, file.file_name);
			recordings = await listCameraRecordings();
		} catch (error) {
			pageError = error instanceof Error ? error.message : 'delete failed';
		}
	}

	function formatBytes(size: number | null): string {
		if (size === null) {
			return '-';
		}

		if (size < 1024) {
			return `${size} B`;
		}

		if (size < 1024 * 1024) {
			return `${(size / 1024).toFixed(1)} KB`;
		}

		return `${(size / (1024 * 1024)).toFixed(1)} MB`;
	}

	function formatDuration(seconds: number | null): string {
		if (seconds === null) {
			return '-';
		}

		const whole = Math.floor(seconds);
		const minutes = Math.floor(whole / 60);
		const rest = whole % 60;
		return `${minutes}:${rest.toString().padStart(2, '0')}`;
	}

	function formatTimestamp(iso: string): string {
		const date = new Date(iso);

		if (Number.isNaN(date.getTime()) === true) {
			return iso;
		}

		return date.toLocaleString();
	}
</script>

{#if loading}
	<p>loading demo state...</p>
{:else if pageError !== ''}
	<ErrorMessage message={pageError} />
{:else}
	<section>
		<h2>cameras</h2>

		{#if cameras.length === 0}
			<p>no cameras available</p>
		{:else}
			<table>
				<thead>
					<tr>
						<th>camera</th>
						<th>status</th>
						<th>recording</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each cameras as camera (camera.id)}
						{@const status = recordingStatus.get(camera.id)}
						{@const isRecording = status?.is_recording === true}
						<tr>
							<td>
								{camera.id}
								<span class="dim">({camera.type})</span>
							</td>
							<td>
								{camera.online === true ? 'online' : 'offline'}
								{camera.running === true ? ' · running' : ''}
							</td>
							<td>
								{#if isRecording}
									<span class="recording-label">
										recording {status?.source ?? ''}
									</span>
									{formatDuration(status?.elapsed_s ?? null)}
									· {formatBytes(status?.size_bytes ?? null)}
								{:else}
									<span class="dim">idle</span>
								{/if}
							</td>
							<td>
								<button
									type="button"
									class="record-toggle"
									class:active={isRecording}
									disabled={pendingAction.has(camera.id)}
									on:click={() => didToggleRecord(camera)}>
									{isRecording ? 'STOP' : 'RECORD'}
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>

	<div class="section-gap"></div>

	<section>
		<h2>recordings</h2>

		{#if recordings.length === 0}
			<p>no recorded files</p>
		{:else}
			<table>
				<thead>
					<tr>
						<th>file</th>
						<th>camera</th>
						<th>size</th>
						<th>duration</th>
						<th>created</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each recordings as file (file.camera_id + file.file_name)}
						<tr>
							<td>{file.file_name}</td>
							<td>{file.camera_id}</td>
							<td>{formatBytes(file.size_bytes)}</td>
							<td>{formatDuration(file.duration_s)}</td>
							<td>{formatTimestamp(file.modified_at)}</td>
							<td class="button-cell">
								<a
									href={buildRecordingDownloadUrl(file.camera_id, file.file_name)}
									target="_blank"
									rel="noopener">
									DOWNLOAD
								</a>
								<button
									type="button"
									disabled={file.is_recording === true}
									on:click={() => didDeleteRecording(file)}>
									DELETE
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>
{/if}

<style>
	.section-gap {
		height: 1.5em;
	}

	.dim {
		opacity: 0.6;
		font-size: 0.9em;
	}

	.recording-label {
		color: #e05020;
		font-weight: 700;
	}

	.record-toggle.active {
		background: #e05020;
		color: white;
	}

	.button-cell {
		white-space: nowrap;
	}

	.button-cell a,
	.button-cell button {
		margin-left: 0.5em;
	}
</style>
