<script lang="ts">
	import { error } from '@sveltejs/kit';
	import { page } from '$app/state';
	import SizerCalibrationTab from '$lib/components/sizer/SizerCalibrationTab.svelte';
	import SizerMeasurementTab from '$lib/components/sizer/SizerMeasurementTab.svelte';
	import SizerStatusTab from '$lib/components/sizer/SizerStatusTab.svelte';
	import { devicePath } from '$lib/nav';

	type SizerView = 'status' | 'measurement' | 'calibration';

	const views: Record<string, SizerView> = {
		status: 'status',
		measurement: 'measurement',
		calibration: 'calibration'
	};

	const selectedView = $derived.by(() => {
		const viewParam = page.params.view;
		const view = viewParam ? views[viewParam] : undefined;

		if (view === undefined) {
			error(404, 'sizer tab not found');
		}

		return view;
	});

	function tabHref(path: string): string {
		return `${devicePath(path)}${page.url.search}`;
	}
</script>

<div class="toggle-group">
	<a class:selected={selectedView === 'status'} href={tabHref('/sizer/status')}>status</a>
	<a class:selected={selectedView === 'measurement'} href={tabHref('/sizer/measurement')}>measurement</a>
	<a class:selected={selectedView === 'calibration'} href={tabHref('/sizer/calibration')}>calibration</a>
</div>

{#if selectedView === 'status'}
	<SizerStatusTab />
{:else if selectedView === 'measurement'}
	<SizerMeasurementTab />
{:else if selectedView === 'calibration'}
	<SizerCalibrationTab />
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
