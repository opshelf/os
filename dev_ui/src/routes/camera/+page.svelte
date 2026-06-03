<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { apiFetch, buildAuthenticatedUrl } from '$lib';
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
	let isImageFlipped = false;

	const cameraFlipStorageKey = 'opshelf.camera.isImageFlipped';
	$: streamUrl =
		selectedCameraId !== '' ? buildAuthenticatedUrl(`/camera/${selectedCameraId}`) : '';
	$: selectedCamera = cameras.find((c) => c.id === selectedCameraId) ?? null;

	onMount(() => {
		if (browser === false) {
			return;
		}

		isImageFlipped = localStorage.getItem(cameraFlipStorageKey) === 'true';
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

	function didToggleImageFlip(): void {
		isImageFlipped = isImageFlipped === false;
		localStorage.setItem(cameraFlipStorageKey, String(isImageFlipped));
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
{:else if statusError !== ''}
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
						<td>consumers</td>
						<td>{selectedCamera.consumers}</td>
					</tr>
				{/if}
			{/if}
		</tbody>
	</table>

	<div style="height: 1.5em"></div>

	<div class="button-row">
		<button type="button" on:click={loadCameras}>refresh</button>
		<button
			type="button"
			class="flip-button"
			class:active={isImageFlipped}
			on:click={didToggleImageFlip}
			aria-pressed={isImageFlipped}>↕︎</button
		>
	</div>

	<div style="height: 1.5em"></div>

	{#if streamUrl !== '' && streamError === false}
		<div class="stream-container">
			{#if selectedCameraId === 'tool'}
				<div class="aim aim-left"></div>
				<div class="aim aim-right"></div>
				<div class="aim aim-top"></div>
				<div class="aim aim-bottom"></div>
			{/if}

			<img
				class:flipped={isImageFlipped}
				src={streamUrl}
				alt="camera feed"
				on:error={didStreamError}
				on:load={didStreamLoad}
			/>
		</div>
	{/if}

	{#if streamError === true}
		<ErrorMessage message="unable to display the camera stream. verify the camera is running." />
	{/if}
{/if}

<style>
	.button-row {
		display: flex;
		gap: 1.5em;
		align-items: center;
	}

	.flip-button {
		min-width: 1.5em;
	}

	.button-row button.active {
		font-weight: 700;
	}

	.stream-container {
		display: block;
		position: relative;
		width: 100%;
		height: auto;
		aspect-ratio: 16 / 9;
	}

	.stream-container img {
		width: 100%;
		height: 100%;
		display: block;
		position: relative;
		z-index: 0;
	}

	.stream-container img.flipped {
		transform: rotate(180deg);
	}

	.aim {
		position: absolute;
		background: red;
		z-index: 1;
	}

	.aim-left {
		bottom: 3%;
		left: 49.75%;
		width: 2px;
		height: 18%;
	}

	.aim-right {
		bottom: 3%;
		left: 58.25%;
		width: 2px;
		height: 18%;
	}

	.aim-top {
		bottom: 18.5%;
		left: 46.5%;
		width: 15%;
		height: 2px;
	}

	.aim-bottom {
		bottom: 5.5%;
		left: 46.5%;
		width: 15%;
		height: 2px;
	}
</style>
