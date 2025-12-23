<script lang="ts">
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import IOBoxCalibration from '$lib/components/calibration/IOBoxCalibration.svelte';
	import RotationCalibration from '$lib/components/calibration/RotationCalibration.svelte';
	import CycleTest from '$lib/components/calibration/CycleTest.svelte';

	let selectedView: 'cell' | 'io' | 'rotation' | 'cycle' = 'cell';

	function didSelectCell(): void {
		selectedView = 'cell';
	}

	function didSelectIO(): void {
		selectedView = 'io';
	}

	function didSelectRotation(): void {
		selectedView = 'rotation';
	}

	function didSelectCycle(): void {
		selectedView = 'cycle';
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
	<div class="divider"></div>
	<button type="button" class:selected={selectedView === 'cycle'} on:click={didSelectCycle}>
		cycle
	</button>
</div>

{#if selectedView === 'cell'}
	<CellCalibration />
{:else if selectedView === 'io'}
	<IOBoxCalibration />
{:else if selectedView === 'rotation'}
	<RotationCalibration />
{:else if selectedView === 'cycle'}
	<CycleTest />
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

	.divider {
		width: 55px;
		height: 1px;
		border-top: 2px black dashed;
	}
</style>
