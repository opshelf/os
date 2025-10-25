<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { apiFetch, buildApiUrl } from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

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
			statusError = error instanceof Error ? error.message : 'unable to load camera status';
		}
	}

	function markStreamError(): void {
		streamError = true;
	}

	function markStreamLoaded(): void {
		streamError = false;
	}
</script>

{#if status?.has_frame === true}
	<img src={streamUrl} alt="camera feed" on:error={markStreamError} on:load={markStreamLoaded} />
{/if}

{#if streamError || status?.has_frame === false}
	<ErrorMessage
		message="unable to display the camera stream. verify the camera service is enabled."
	/>
{/if}

{#if statusError}
	<ErrorMessage message={statusError} />
{/if}

{#if status}
	<table>
		<thead>
			<tr>
				<th colspan="2">stream status</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>enabled</td>
				<td>{status.enabled ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>running</td>
				<td>{status.running ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>consumers</td>
				<td>{status.consumers ?? 0}</td>
			</tr>
			<tr>
				<td>target fps</td>
				<td>{status.target_fps ?? ''}</td>
			</tr>
			<tr>
				<td>latest frame</td>
				<td>{status.has_frame ? 'available' : 'not available'}</td>
			</tr>
			{#if status.message}
				<tr>
					<td>message</td>
					<td>{status.message}</td>
				</tr>
			{/if}
		</tbody>
	</table>
{/if}

<br />

<button type="button" on:click={loadStatus}>refresh status</button>
