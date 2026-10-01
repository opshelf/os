<script lang="ts">
	import { page } from '$app/state';
	import StorageLayoutEditor from '$lib/components/calibration/StorageLayoutEditor.svelte';
	import CellCalibration from '$lib/components/calibration/CellCalibration.svelte';
	import CellAutoCalibration from '$lib/components/calibration/CellAutoCalibration.svelte';
	import CellCalibrationTest from '$lib/components/calibration/CellCalibrationTest.svelte';
	import IOBoxCalibration from '$lib/components/calibration/IOBoxCalibration.svelte';
	import RotationCalibration from '$lib/components/calibration/RotationCalibration.svelte';
	import { devicePath } from '$lib/nav';

	type CalibrationView = 'layout' | 'cell_auto' | 'cell_manual' | 'io' | 'rotation' | 'test';

	let { selectedView = 'layout' }: { selectedView?: CalibrationView } = $props();

	const sideParam = page.url.searchParams.get('side');

	const initialSide =
		sideParam === 'front_module' || sideParam === 'back_module' ? sideParam : undefined;

	const initialShelf = page.url.searchParams.has('shelf')
		? parseInt(page.url.searchParams.get('shelf')!, 10)
		: undefined;

	const initialCell = page.url.searchParams.has('cell')
		? parseInt(page.url.searchParams.get('cell')!, 10)
		: undefined;

	function tabHref(path: string): string {
		return `${devicePath(path)}${page.url.search}`;
	}
</script>

<div class="toggle-group">
	<a class:selected={selectedView === 'layout'} href={tabHref('/storage/layout')}>layout</a>
	<a class:selected={selectedView === 'cell_auto'} href={tabHref('/storage/auto')}>auto</a>
	<a class:selected={selectedView === 'cell_manual'} href={tabHref('/storage/cells')}>manual</a>
	<a class:selected={selectedView === 'io'} href={tabHref('/storage/io-box')}>io box</a>
	<a class:selected={selectedView === 'rotation'} href={tabHref('/storage/tool-rotation')}>
		rotation
	</a>
	<a class:selected={selectedView === 'test'} href={tabHref('/storage/test')}>test</a>
</div>

{#if selectedView === 'layout'}
	<StorageLayoutEditor />
{:else if selectedView === 'cell_manual'}
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

	.toggle-group a {
		border: 2px solid black;
		background: #fff;
		cursor: pointer;
		margin: 0;
		color: black;
		text-decoration: none;
	}

	.toggle-group a.selected {
		background: black;
		color: #fff;
	}
</style>
