<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getStorageGrid,
		moveToCell,
		jogAxis,
		testGripper,
		saveCalibration,
		type StorageGrid,
		type Position
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import ShelfCalibration from '$lib/components/calibration/ShelfCalibration.svelte';

	let storageGrid: StorageGrid | null = null;
	let loading = true;
	let loadError = '';

	let selectedSide: 'left_module' | 'right_module' = 'left_module';
	let selectedShelf = 0;
	let selectedCell = 0;

	let currentPosition: Position | null = null;
	let isMoving = false;
	let isTesting = false;
	let isSaving = false;
	let error = '';
	let message = '';

	let jogStep = 1;

	const JOG_STEPS = [
		{ value: 0.1, label: '0.1mm' },
		{ value: 0.25, label: '0.25mm' },
		{ value: 0.5, label: '0.5mm' },
		{ value: 1, label: '1mm' },
		{ value: 2, label: '2mm' },
		{ value: 5, label: '5mm' },
		{ value: 10, label: '1cm' },
		{ value: 20, label: '2cm' },
		{ value: 50, label: '5cm' },
		{ value: 100, label: '10cm' }
	];

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadStorage();
	});

	async function loadStorage(): Promise<void> {
		loading = true;
		loadError = '';

		try {
			storageGrid = await getStorageGrid();
			selectedShelf = 0;
			selectedCell = 0;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'failed to load storage grid';
		} finally {
			loading = false;
		}
	}

	function getShelves() {
		if (storageGrid === null) {
			return [];
		}

		if (selectedSide === 'left_module') {
			return storageGrid.left_module;
		}

		return storageGrid.right_module;
	}

	function getCells() {
		const shelves = getShelves();

		if (selectedShelf >= shelves.length) {
			return [];
		}

		return shelves[selectedShelf].cells;
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'left_module' | 'right_module';
		selectedShelf = 0;
		selectedCell = 0;
		currentPosition = null;
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		selectedCell = 0;
		currentPosition = null;
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = parseInt(target.value, 10);
		currentPosition = null;
		error = '';
		message = '';
	}

	async function didClickCalibrate(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';

		try {
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);
			message = 'moved to cell';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to move to cell';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<void> {
		isMoving = true;
		error = '';

		try {
			currentPosition = await jogAxis(axis, delta);
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to jog axis';
		} finally {
			isMoving = false;
		}
	}

	async function didClickTest(): Promise<void> {
		isTesting = true;
		error = '';
		message = '';

		try {
			await testGripper(selectedSide, selectedShelf, selectedCell);
			message = 'gripper test completed';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to test gripper';
		} finally {
			isTesting = false;
		}
	}

	async function didClickSave(): Promise<void> {
		if (currentPosition === null) {
			error = 'no position to save';
			return;
		}

		isSaving = true;
		error = '';
		message = '';

		try {
			await saveCalibration(
				selectedSide,
				selectedShelf,
				selectedCell,
				currentPosition.x,
				currentPosition.y,
				currentPosition.z_offset,
				currentPosition.slide_extension
			);
			message = 'calibration saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save calibration';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';

		try {
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);
			message = 'position reset';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to reset position';
		} finally {
			isMoving = false;
		}
	}

	function formatNumber(value: number): string {
		return value.toFixed(2);
	}
</script>

{#if loading === true}
	<p>loading storage grid...</p>
{:else if loadError !== ''}
	<ErrorMessage message={loadError} />
{:else if storageGrid !== null}
	<table>
		<thead>
			<tr>
				<th colspan="3">cell selection</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>
					<span class="field-label">side</span>
					<select value={selectedSide} on:change={didChangeSide}>
						<option value="left_module">left module</option>
						<option value="right_module">right module</option>
					</select>
				</td>
				<td>
					<span class="field-label">shelf</span>
					<select value={selectedShelf} on:change={didChangeShelf}>
						{#each getShelves() as _, index}
							<option value={index}>{index}</option>
						{/each}
					</select>
				</td>
				<td>
					<span class="field-label">cell</span>
					<select value={selectedCell} on:change={didChangeCell}>
						{#each getCells() as _, index}
							<option value={index}>{index}</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<button
						class="calibrate-button"
						type="button"
						on:click={didClickCalibrate}
						disabled={isMoving === true || isTesting === true}
					>
						calibrate
					</button>
				</td>
			</tr>
		</tbody>
	</table>

	{#if currentPosition !== null}
		<table class="position-table">
			<thead>
				<tr>
					<th colspan="3">current position</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>x: {formatNumber(currentPosition.x)}</td>
					<td>y: {formatNumber(currentPosition.y)}</td>
					<td>z: {formatNumber(currentPosition.z_offset)}</td>
				</tr>
			</tbody>
		</table>

		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">X</span>
				<button
					type="button"
					on:click={() => didClickJog('x', -jogStep)}
					disabled={isMoving === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('x', jogStep)}
					disabled={isMoving === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">Y</span>
				<button
					type="button"
					on:click={() => didClickJog('y', -jogStep)}
					disabled={isMoving === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('y', jogStep)}
					disabled={isMoving === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">Z</span>
				<button
					type="button"
					on:click={() => didClickJog('z', -jogStep)}
					disabled={isMoving === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('z', jogStep)}
					disabled={isMoving === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-step">
				<span class="axis-label">step</span>
				<select bind:value={jogStep}>
					{#each JOG_STEPS as step}
						<option value={step.value}>{step.label}</option>
					{/each}
				</select>
			</div>
		</div>

		<div class="action-buttons">
			<button
				type="button"
				on:click={didClickTest}
				disabled={isMoving === true || isTesting === true || isSaving === true}
			>
				test gripper
			</button>
			<button
				type="button"
				on:click={didClickReset}
				disabled={isMoving === true || isTesting === true || isSaving === true}
			>
				reset
			</button>
			<button
				type="button"
				on:click={didClickSave}
				disabled={isMoving === true || isTesting === true || isSaving === true}
			>
				save
			</button>
		</div>
	{/if}

	{#if message !== ''}
		<div class="message">{message}</div>
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}

	<div class="shelf-section">
		<ShelfCalibration />
	</div>
{/if}

<style>
	.shelf-section {
		margin-top: 2em;
		padding-top: 2em;
		border-top: 2px dashed #000;
	}

	.field-label {
		margin-right: 0.5em;
	}

	.calibrate-button {
		margin-top: 0;
	}

	.position-table {
		margin-top: 1em;
	}

	.jog-controls {
		display: flex;
		gap: 2em;
		margin-top: 2em;
		margin-bottom: 1em;
	}

	.jog-axis,
	.jog-step {
		display: flex;
		align-items: center;
		gap: 0.5em;
	}

	.axis-label {
		font-weight: bold;
		min-width: 1.5em;
	}

	.jog-axis button {
		min-width: 3em;
		min-height: 3em;
		font-size: 1.5em;
		margin-top: 0;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1em;
	}

	.message {
		margin-top: 1em;
	}
</style>
