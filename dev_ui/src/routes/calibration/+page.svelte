<script lang="ts">
	import { page } from '$app/state';
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import CellAutoCalibration from '$lib/components/calibration/CellAutoCalibration.svelte';
	import CellCalibrationTest from '$lib/components/calibration/CellCalibrationTest.svelte';
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

	let selectedView: 'cell_auto' | 'cell_manual' | 'io' | 'rotation' | 'test' = 'cell_auto';

	function didSelectCellManual(): void {
		selectedView = 'cell_manual';
	}

	function didSelectCellAuto(): void {
		selectedView = 'cell_auto';
	}

	function didSelectTest(): void {
		selectedView = 'test';
	}

	function didSelectIO(): void {
		selectedView = 'io';
	}

	function didSelectRotation(): void {
		selectedView = 'rotation';
	}
</script>

<div class="toggle-group">
	<button type="button" class:selected={selectedView === 'cell_auto'} on:click={didSelectCellAuto}>
		auto
	</button>
	<button
		type="button"
		class:selected={selectedView === 'cell_manual'}
		on:click={didSelectCellManual}
	>
		cells
	</button>
	<button type="button" class:selected={selectedView === 'io'} on:click={didSelectIO}>
		io box
	</button>
	<button type="button" class:selected={selectedView === 'rotation'} on:click={didSelectRotation}>
		tool rotation
	</button>
	<button type="button" class:selected={selectedView === 'test'} on:click={didSelectTest}>
		test
	</button>
</div>

{#if selectedView === 'cell_manual'}
	<CellCalibration {initialSide} {initialShelf} {initialCell} />
{:else if selectedView === 'cell_auto'}
	<CellAutoCalibration />
{:else if selectedView === 'test'}
	<CellCalibrationTest {initialSide} {initialShelf} {initialCell} />
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
