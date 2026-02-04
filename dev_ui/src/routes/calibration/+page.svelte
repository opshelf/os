<script lang="ts">
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import IOBoxCalibration from '$lib/components/calibration/IOBoxCalibration.svelte';
	import RotationCalibration from '$lib/components/calibration/RotationCalibration.svelte';

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
	<CellCalibration />
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
