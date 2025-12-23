<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import { getStorageGrid, runCycleTest, type StorageGrid } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let storageGrid: StorageGrid | null = null;
	let loading = true;
	let loadError = '';

	let selectedSide: 'left_module' | 'right_module' = 'left_module';
	let selectedShelf = 0;
	let selectedCell = 0;

	let isRunning = false;
	let error = '';
	let message = '';

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
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		selectedCell = 0;
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = parseInt(target.value, 10);
		error = '';
		message = '';
	}

	async function didClickRunCycle(): Promise<void> {
		isRunning = true;
		error = '';
		message = '';

		try {
			await runCycleTest(selectedSide, selectedShelf, selectedCell);
			message = 'cycle test completed successfully';
		} catch (err) {
			error = err instanceof Error ? err.message : 'cycle test failed';
		} finally {
			isRunning = false;
		}
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
						class="run-cycle-button"
						type="button"
						on:click={didClickRunCycle}
						disabled={isRunning === true}
					>
						{isRunning === true ? 'running...' : 'run cycle'}
					</button>
				</td>
			</tr>
		</tbody>
	</table>

	<br /><br />

	{#if message !== ''}
		<p class="message">{message}</p>
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	.field-label {
		margin-right: 0.5em;
	}

	.run-cycle-button {
		margin-top: 0;
	}

	.message {
		margin-top: 1em;
	}
</style>
