<script lang="ts">
	import { page } from '$app/state';
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import IOBoxCalibration from '$lib/components/calibration/IOBoxCalibration.svelte';
	import RotationCalibration from '$lib/components/calibration/RotationCalibration.svelte';

	const sideParam = page.url.searchParams.get('side');

	const initialSide =
		sideParam === 'left_module' || sideParam === 'right_module' ? sideParam : undefined;

	const initialShelf = page.url.searchParams.has('shelf')
		? parseInt(page.url.searchParams.get('shelf')!, 10)
		: undefined;

	const initialCell = page.url.searchParams.has('cell')
		? parseInt(page.url.searchParams.get('cell')!, 10)
		: undefined;

	let selectedView: 'cell' | 'io' | 'rotation' = 'cell';

	function didSelectCell(): void {
		selectedView = 'cell';
	}

	function didSelectIO(): void {
		selectedView = 'io';
	}

	function didSelectRotation(): void {
		selectedView = 'rotation';
	}
</script>

<div class="toggle-group">
	<button type="button" class:selected={selectedView === 'cell'} on:click={didSelectCell}>
		cell
	</button>
	<button type="button" class:selected={selectedView === 'io'} on:click={didSelectIO}>
		io box
	</button>
	<button type="button" class:selected={selectedView === 'rotation'} on:click={didSelectRotation}>
		rotation
	</button>
</div>

{#if selectedView === 'cell'}
	<CellCalibration {initialSide} {initialShelf} {initialCell} />
{:else if selectedView === 'io'}
	<IOBoxCalibration />
{:else if selectedView === 'rotation'}
	<RotationCalibration />
{/if}

<style>
	.toggle-group {
		display: inline-flex;
		margin-bottom: 1.5em;
		gap: 1em;
		align-items: center;
	}

	.toggle-group button {
		border: 2px solid black;
		background: #fff;
		cursor: pointer;
		margin: 0;
	}

	.toggle-group button.selected {
		background: black;
		color: #fff;
	}
</style>
