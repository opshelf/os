<script lang="ts">
	import { browser } from '$app/environment';
	import { onDestroy, onMount } from 'svelte';
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

	type CameraLine = [number, number, number, number];
	type Point = {
		x: number;
		y: number;
	};

	let cameras: CameraInfo[] = [];
	let selectedCameraId: string = '';
	let statusError = '';
	let streamError = false;
	let loading = true;
	let isImageFlipped = false;
	let isDrawingMode = false;
	let canvasEl: HTMLCanvasElement | null = null;
	let resizeObserver: ResizeObserver | null = null;
	let observedCanvas: HTMLCanvasElement | null = null;
	let cameraLines: CameraLine[] = [];
	let lineStart: Point | null = null;
	let previewEnd: Point | null = null;

	const cameraFlipStorageKey = 'opshelf.camera.isImageFlipped';
	const cameraLinesStorageKey = 'opshelf.camera.lines';

	$: streamUrl =
		selectedCameraId !== '' ? buildAuthenticatedUrl(`/camera/${selectedCameraId}`) : '';
	$: selectedCamera = cameras.find((c) => c.id === selectedCameraId) ?? null;
	$: if (browser === true && selectedCameraId !== '') {
		loadCameraLines(selectedCameraId);
	}
	$: if (browser === true && canvasEl !== null) {
		observeCanvas(canvasEl);
	}
	onMount(() => {
		if (browser === false) {
			return;
		}

		isImageFlipped = localStorage.getItem(cameraFlipStorageKey) === 'true';
		loadCameras();
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
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
		lineStart = null;
		previewEnd = null;
	}

	function didToggleImageFlip(): void {
		isImageFlipped = isImageFlipped === false;
		localStorage.setItem(cameraFlipStorageKey, String(isImageFlipped));
	}

	function didToggleDrawingMode(): void {
		isDrawingMode = isDrawingMode === false;
		lineStart = null;
		previewEnd = null;
		drawLines();
	}

	function didClearLastLine(): void {
		if (cameraLines.length === 0) {
			return;
		}

		cameraLines = cameraLines.slice(0, -1);
		lineStart = null;
		previewEnd = null;
		saveCameraLines();
		drawLines();
	}

	function didStreamError(): void {
		streamError = true;
	}

	function didStreamLoad(): void {
		streamError = false;
		requestAnimationFrame(() => {
			resizeCanvas();
			drawLines();
		});
	}

	function didCanvasClick(event: MouseEvent): void {
		if (isDrawingMode === false || canvasEl === null) {
			return;
		}

		const point = getCanvasPoint(event);

		if (lineStart === null) {
			lineStart = point;
			previewEnd = point;
			drawLines();
			return;
		}

		cameraLines = [...cameraLines, [lineStart.x, lineStart.y, point.x, point.y]];
		lineStart = null;
		previewEnd = null;
		saveCameraLines();
		drawLines();
	}

	function didCanvasMouseMove(event: MouseEvent): void {
		if (isDrawingMode === false || lineStart === null || canvasEl === null) {
			return;
		}

		previewEnd = getCanvasPoint(event);
		drawLines();
	}

	function observeCanvas(canvas: HTMLCanvasElement): void {
		if (observedCanvas === canvas) {
			return;
		}

		resizeObserver?.disconnect();
		observedCanvas = canvas;
		resizeObserver = new ResizeObserver(() => {
			resizeCanvas();
			drawLines();
		});
		resizeObserver.observe(canvas);
		resizeCanvas();
		drawLines();
	}

	function resizeCanvas(): void {
		if (canvasEl === null) {
			return;
		}

		const { width, height } = canvasEl.getBoundingClientRect();
		const nextWidth = Math.max(1, Math.round(width));
		const nextHeight = Math.max(1, Math.round(height));

		if (canvasEl.width === nextWidth && canvasEl.height === nextHeight) {
			return;
		}

		canvasEl.width = nextWidth;
		canvasEl.height = nextHeight;
	}

	function getCanvasPoint(event: MouseEvent): Point {
		const rect = canvasEl?.getBoundingClientRect();

		if (rect === undefined) {
			return { x: 0, y: 0 };
		}

		if (isImageFlipped === true) {
			return {
				x: rect.right - event.clientX,
				y: rect.bottom - event.clientY
			};
		}

		return {
			x: event.clientX - rect.left,
			y: event.clientY - rect.top
		};
	}

	function drawLines(): void {
		if (canvasEl === null) {
			return;
		}

		const context = canvasEl.getContext('2d');

		if (context === null) {
			return;
		}

		context.clearRect(0, 0, canvasEl.width, canvasEl.height);
		context.strokeStyle = '#00e5ff';
		context.lineWidth = 2;
		context.lineCap = 'round';

		for (const line of cameraLines) {
			context.beginPath();
			context.moveTo(line[0], line[1]);
			context.lineTo(line[2], line[3]);
			context.stroke();
		}

		if (lineStart !== null && previewEnd !== null) {
			context.setLineDash([6, 6]);
			context.beginPath();
			context.moveTo(lineStart.x, lineStart.y);
			context.lineTo(previewEnd.x, previewEnd.y);
			context.stroke();
			context.setLineDash([]);
		}
	}

	function loadCameraLines(cameraId: string): void {
		const allLines = readStoredCameraLines();
		cameraLines = allLines[cameraId] ?? [];
		requestAnimationFrame(() => {
			resizeCanvas();
			drawLines();
		});
	}

	function saveCameraLines(): void {
		if (browser === false || selectedCameraId === '') {
			return;
		}

		const allLines = readStoredCameraLines();
		allLines[selectedCameraId] = cameraLines;
		localStorage.setItem(cameraLinesStorageKey, JSON.stringify(allLines));
	}

	function readStoredCameraLines(): Record<string, CameraLine[]> {
		if (browser === false) {
			return {};
		}

		const storedLines = localStorage.getItem(cameraLinesStorageKey);

		if (storedLines === null) {
			return {};
		}

		try {
			const parsed = JSON.parse(storedLines);

			if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
				return {};
			}

			return parsed as Record<string, CameraLine[]>;
		} catch {
			return {};
		}
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
		<button
			type="button"
			class="draw-button"
			class:active={isDrawingMode}
			on:click={didToggleDrawingMode}
			aria-pressed={isDrawingMode}>DRAW</button
		>
		<button type="button" on:click={didClearLastLine} disabled={cameraLines.length === 0}>clear</button>
	</div>

	<div style="height: 1.5em"></div>

	{#if streamUrl !== '' && streamError === false}
		<div class="stream-container">
			<img
				class:flipped={isImageFlipped}
				src={streamUrl}
				alt="camera feed"
				on:error={didStreamError}
				on:load={didStreamLoad}
			/>
			<canvas
				bind:this={canvasEl}
				class:drawing={isDrawingMode}
				class:flipped={isImageFlipped}
				on:click={didCanvasClick}
				on:mousemove={didCanvasMouseMove}
				aria-label="camera drawing overlay"
			></canvas>
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

	.draw-button.active {
		background-color: #e6e6e6;
		box-shadow: none;
		transform: translate3d(4px, 4px, 0px);
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

	.stream-container img.flipped,
	.stream-container canvas.flipped {
		transform: rotate(180deg);
	}

	.stream-container canvas {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 100%;
		z-index: 1;
		pointer-events: none;
	}

	.stream-container canvas.drawing {
		cursor: crosshair;
		pointer-events: auto;
	}
</style>
