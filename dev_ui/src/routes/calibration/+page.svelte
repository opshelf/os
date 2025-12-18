<script lang="ts">
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import IOBoxCalibration from '$lib/components/calibration/IOBoxCalibration.svelte';

	let selectedView: 'cell' | 'io' = 'cell';

	function didSelectCell(): void {
		selectedView = 'cell';
	}

	function didSelectIO(): void {
		selectedView = 'io';
	}
</script>

<div class="toggle-group">
	<button type="button" class:selected={selectedView === 'cell'} on:click={didSelectCell}>
		cell
	</button>
	<button type="button" class:selected={selectedView === 'io'} on:click={didSelectIO}>
		io box
	</button>
</div>

{#if selectedView === 'cell'}
	<CellCalibration />
{:else}
	<IOBoxCalibration />
{/if}

<style>
	.toggle-group {
		display: inline-flex;
		margin-bottom: 1.5em;
		gap: 1em;
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
