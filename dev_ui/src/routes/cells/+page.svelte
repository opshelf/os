<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getStorageGrid,
		updateCellsBatchStatus,
		runCycleTest,
		CELL_STATUSES,
		type StorageGrid,
		type CellIdentifier
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let grid: StorageGrid | null = $state(null);
	let selectedCells: Set<string> = $state(new Set());
	let selectedStatus: string = $state(CELL_STATUSES[0]);
	let isLoading = $state(true);
	let isApplying = $state(false);
	let isCycling = $state(false);
	let error = $state('');
	let message = $state('');

	const isDisabled = $derived.by(function () {
		if (selectedCells.size === 0) {
			return true;
		}
		if (isApplying === true) {
			return true;
		}
		if (isCycling === true) {
			return true;
		}
		return false;
	});

	function cellKey(side: string, shelfIdx: number, cellIdx: number): string {
		return `${side}:${shelfIdx}:${cellIdx}`;
	}

	function parseCellKey(key: string): CellIdentifier {
		const [side, shelf, cell] = key.split(':');
		return { side, shelf_idx: parseInt(shelf, 10), cell_idx: parseInt(cell, 10) };
	}

	let anchorCell: { side: string; shelfIdx: number; cellIdx: number } | null = $state(null);

	function isCellSelected(side: string, shelfIdx: number, cellIdx: number): boolean {
		return selectedCells.has(cellKey(side, shelfIdx, cellIdx));
	}

	function regionKeys(
		from: { side: string; shelfIdx: number; cellIdx: number },
		to: { side: string; shelfIdx: number; cellIdx: number }
	): Set<string> {
		const keys = new Set<string>();

		if (grid === null) {
			return keys;
		}

		const sides = from.side === to.side
			? [from.side]
			: ['left_module', 'right_module'];

		for (const side of sides) {
			const shelves = side === 'left_module' ? grid.left_module : grid.right_module;
			const minShelf = Math.min(from.shelfIdx, to.shelfIdx);
			const maxShelf = Math.max(from.shelfIdx, to.shelfIdx);
			const minCell = Math.min(from.cellIdx, to.cellIdx);
			const maxCell = Math.max(from.cellIdx, to.cellIdx);

			for (let s = minShelf; s <= maxShelf && s < shelves.length; s++) {
				for (let c = minCell; c <= maxCell && c < shelves[s].cells.length; c++) {
					keys.add(cellKey(side, s, c));
				}
			}
		}

		return keys;
	}

	function didClickCell(side: string, shelfIdx: number, cellIdx: number, event: MouseEvent): void {
		const key = cellKey(side, shelfIdx, cellIdx);
		const isCtrl = event.ctrlKey === true || event.metaKey === true;

		if (isCtrl === true) {
			const next = new Set(selectedCells);

			if (next.has(key) === true) {
				next.delete(key);
			} else {
				next.add(key);
			}

			selectedCells = next;
			anchorCell = { side, shelfIdx, cellIdx };
			return;
		}

		if (event.shiftKey === true && anchorCell !== null) {
			const region = regionKeys(anchorCell, { side, shelfIdx, cellIdx });
			selectedCells = new Set([...selectedCells, ...region]);
			return;
		}

		if (selectedCells.has(key) === true && selectedCells.size === 1) {
			selectedCells = new Set();
			anchorCell = null;
			return;
		}

		selectedCells = new Set([key]);
		anchorCell = { side, shelfIdx, cellIdx };
	}

	function didClickSelectAll(): void {
		if (grid === null) {
			return;
		}

		const all = new Set<string>();

		for (const [side, shelves] of [['left_module', grid.left_module], ['right_module', grid.right_module]] as const) {
			for (let shelfIdx = 0; shelfIdx < shelves.length; shelfIdx++) {
				for (let cellIdx = 0; cellIdx < shelves[shelfIdx].cells.length; cellIdx++) {
					all.add(cellKey(side, shelfIdx, cellIdx));
				}
			}
		}

		selectedCells = all;
	}

	async function didClickApply(): Promise<void> {
		if (selectedCells.size === 0) {
			return;
		}

		isApplying = true;
		error = '';
		message = '';

		const cells = Array.from(selectedCells).map(parseCellKey);

		try {
			await updateCellsBatchStatus(cells, selectedStatus);
			message = `updated ${cells.length} cell${cells.length > 1 ? 's' : ''} to ${selectedStatus}`;
			selectedCells = new Set();
			grid = await getStorageGrid();
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to update cells';
		} finally {
			isApplying = false;
		}
	}

	async function didClickCycleTest(): Promise<void> {
		if (selectedCells.size === 0) {
			return;
		}

		isCycling = true;
		error = '';
		message = '';

		const cells = Array.from(selectedCells).map(parseCellKey);

		let currentCell = '';

		try {
			for (const cell of cells) {
				currentCell = `${cell.shelf_idx}:${cell.cell_idx}`;
				await runCycleTest(cell.side, cell.shelf_idx, cell.cell_idx);
			}

			message = `cycle test passed for ${cells.length} cell${cells.length > 1 ? 's' : ''}`;
		} catch (err) {
			const detail = err instanceof Error ? err.message : 'unknown error';
			error = `cycle test failed at ${currentCell}: ${detail}`;
		} finally {
			isCycling = false;
		}
	}

	function statusColor(status: string): string {
		switch (status) {
			case 'occupied':
				return '#e0e0e0';
			case 'maintenance':
				return '#fff3cd';
			case 'disabled':
				return '#f8d7da';
			default:
				return 'transparent';
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			grid = await getStorageGrid();
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load storage grid';
		} finally {
			isLoading = false;
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		initialLoad();
	});
</script>

{#if isLoading === true}
	<p>loading storage grid...</p>
{:else if grid !== null}
	{#each [{ label: 'left module', side: 'left_module', shelves: grid.left_module }, { label: 'right module', side: 'right_module', shelves: grid.right_module }] as module}
		<section class="module">
			<h2>{module.label}</h2>

			<div class="shelves">
				{#each module.shelves as shelf, shelfIdx}
					<div class="shelf-row">
						{#each [...shelf.cells].reverse() as cell, reverseCellIdx}
							{@const cellIdx = shelf.cells.length - 1 - reverseCellIdx}
							<button
								type="button"
								class="cell"
								class:selected={isCellSelected(module.side, shelfIdx, cellIdx)}
								style="background-color: {statusColor(cell.status)};"
								onclick={(e) => didClickCell(module.side, shelfIdx, cellIdx, e)}
							>
								{shelfIdx}:{cellIdx}
							</button>
						{/each}
					</div>
				{/each}
			</div>
		</section>
	{/each}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}

	<div class="toolbar-spacer"></div>

	<div class="controls">
		<select bind:value={selectedStatus}>
			{#each CELL_STATUSES as status}
				<option value={status}>{status}</option>
			{/each}
		</select>

		<button type="button" disabled={isDisabled} onclick={didClickApply}>
			apply
		</button>

		<button type="button" onclick={didClickSelectAll}>
			select all
		</button>

		<button
			type="button"
			disabled={selectedCells.size === 0 || isCycling === true}
			onclick={didClickCycleTest}
		>
			cycle test
		</button>

	</div>
{:else if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.module {
		margin-bottom: 2em;
	}

	h2 {
		text-transform: uppercase;
		margin-bottom: 0.5em;
		border-bottom: 2px solid black;
		padding-bottom: 0.25em;
	}

	.shelves {
		display: flex;
		flex-direction: column-reverse;
	}

	.shelf-row {
		display: flex;
		gap: 2px;
		margin-bottom: 2px;
	}

	.cell {
		width: 60px;
		height: 60px;
		border: 2px solid black;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		padding: 0;
		margin: 0;
		box-shadow: none;
		text-transform: none;
		font-size: 0.85em;
		direction: ltr;
	}

	.cell:active {
		transform: none;
	}

	.cell.selected {
		border: 3px solid oklch(0.5318 0.2399 256.98999);
		outline: 2px solid oklch(0.5318 0.2399 256.98999);
	}

	.controls {
		display: flex;
		align-items: center;
		gap: 1em;
		position: fixed;
		bottom: 2em;
		left: 2em;
		width: fit-content;
		padding: 1em;
		background-color: white;
		border: 3px solid black;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		z-index: 100;
	}

	.controls button {
		margin-top: 0;
	}

	.controls select {
		padding: 5px;
		border: 2px solid black;
		background-color: white;
		text-transform: uppercase;
		font-family: inherit;
	}

	.toolbar-spacer {
		height: 5em;
	}
</style>
