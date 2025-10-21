<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { apiFetch, buildApiUrl } from '$lib';

	type CameraStatus = {
		enabled?: boolean;
		running?: boolean;
		consumers?: number;
		target_fps?: number;
		url?: string;
		has_frame?: boolean;
		message?: string;
	};

	const streamUrl = buildApiUrl('/camera');

	let status: CameraStatus | null = null;
	let statusError = '';
	let streamError = false;

	onMount(() => {
		if (!browser) {
			return;
		}
		loadStatus();
	});

	async function loadStatus(): Promise<void> {
		try {
			status = await apiFetch<CameraStatus>('/camera/status');
			statusError = '';
		} catch (error) {
			statusError = error instanceof Error ? error.message : 'Unable to load camera status';
		}
	}

	function markStreamError(): void {
		streamError = true;
	}

	function markStreamLoaded(): void {
		streamError = false;
	}
</script>

<h1>Camera Feed</h1>

<p>The live feed below connects to the API's <code>/api/camera</code> endpoint.</p>

<img src={streamUrl} alt="Camera feed" on:error={markStreamError} on:load={markStreamLoaded} />

{#if streamError}
	<p>Unable to display the camera stream. Verify the camera service is enabled.</p>
{/if}

<button type="button" on:click={loadStatus}>Refresh Status</button>

{#if statusError}
	<p>{statusError}</p>
{:else if status}
	<h2>Stream Status</h2>
	<table>
		<tbody>
			<tr>
				<td>Enabled</td>
				<td>{status.enabled ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>Running</td>
				<td>{status.running ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>Consumers</td>
				<td>{status.consumers ?? 0}</td>
			</tr>
			<tr>
				<td>Target FPS</td>
				<td>{status.target_fps ?? ''}</td>
			</tr>
			<tr>
				<td>Camera URL</td>
				<td>{status.url ?? ''}</td>
			</tr>
			<tr>
				<td>Latest Frame</td>
				<td>{status.has_frame ? 'available' : 'not available'}</td>
			</tr>
			{#if status.message}
				<tr>
					<td>Message</td>
					<td>{status.message}</td>
				</tr>
			{/if}
		</tbody>
	</table>
{/if}
