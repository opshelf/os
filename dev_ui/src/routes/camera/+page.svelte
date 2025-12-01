<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { apiFetch, buildApiUrl } from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	type CameraInfo = {
		id: string;
		type: string;
		running: boolean;
		consumers: number;
		has_frame: boolean;
	};

	type CamerasResponse = {
		cameras: CameraInfo[];
	};

	let cameras: CameraInfo[] = [];
	let selectedCameraId: string = '';
	let statusError = '';
	let streamError = false;
	let loading = true;

	$: streamUrl = selectedCameraId !== '' ? buildApiUrl(`/camera/${selectedCameraId}`) : '';
	$: selectedCamera = cameras.find((c) => c.id === selectedCameraId) ?? null;

	onMount(() => {
		if (browser === false) {
			return;
		}
		loadCameras();
	});

	async function loadCameras(): Promise<void> {
		loading = true;
		try {
			const response = await apiFetch<CamerasResponse>('/camera');
			cameras = response.cameras;
			statusError = '';

			if (selectedCameraId === '' && cameras.length > 0) {
				selectedCameraId = cameras[0].id;
			}
		} catch (error) {
			statusError = error instanceof Error ? error.message : 'unable to load cameras';
		} finally {
			loading = false;
		}
	}

	function didSelectCamera(event: Event): void {
		const target = event.target as HTMLSelectElement;
		selectedCameraId = target.value;
		streamError = false;
	}

	function didStreamError(): void {
		streamError = true;
	}

	function didStreamLoad(): void {
		streamError = false;
	}
</script>

{#if loading}
	<p>loading cameras...</p>
{:else}
	{#if statusError !== ''}
		<ErrorMessage message={statusError} />
	{:else}
		<table>
			<thead>
				<tr>
					<th colspan="2">camera selection</th>
				</tr>
			</thead>
			<tbody>
				{#if cameras.length === 0}
					<tr>
						<td colspan="2">no cameras available</td>
					</tr>
				{:else}
					<tr>
						<td>camera</td>
						<td>
							<select value={selectedCameraId} on:change={didSelectCamera}>
								{#each cameras as camera}
									<option value={camera.id}>{camera.id} ({camera.type})</option>
								{/each}
							</select>
						</td>
					</tr>
					{#if selectedCamera !== null}
						<tr>
							<td>type</td>
							<td>{selectedCamera.type}</td>
						</tr>
						<tr>
							<td>running</td>
							<td>{selectedCamera.running === true ? 'yes' : 'no'}</td>
						</tr>
						<tr>
							<td>consumers</td>
							<td>{selectedCamera.consumers}</td>
						</tr>
						<tr>
							<td>has frame</td>
							<td>{selectedCamera.has_frame === true ? 'yes' : 'no'}</td>
						</tr>
					{/if}
				{/if}
			</tbody>
		</table>

		<button type="button" on:click={loadCameras}>refresh</button>

		{#if streamUrl !== '' && streamError === false}
			<img src={streamUrl} alt="camera feed" on:error={didStreamError} on:load={didStreamLoad} />
		{/if}

		{#if streamError === true}
			<ErrorMessage
				message="unable to display the camera stream. verify the camera is running."
			/>
		{/if}
	{/if}
{/if}
