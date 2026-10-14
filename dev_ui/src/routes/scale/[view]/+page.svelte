<script lang="ts">
	import { error } from '@sveltejs/kit';
	import { page } from '$app/state';
	import ScaleTab from '$lib/components/scale/ScaleTab.svelte';
	import { devicePath } from '$lib/nav';

	type ScaleView = 'status' | 'calibration';

	const views: Record<string, ScaleView> = {
		status: 'status',
		calibration: 'calibration'
	};

	const selectedView = $derived.by(() => {
		const viewParam = page.params.view;
		const view = viewParam ? views[viewParam] : undefined;

		if (view === undefined) {
			error(404, 'scale tab not found');
		}

		return view;
	});

	function tabHref(path: string): string {
		return `${devicePath(path)}${page.url.search}`;
	}
</script>

<svelte:head>
	<title>Scale • Openshelf</title>
</svelte:head>

<div class="toggle-group">
	<a class:selected={selectedView === 'status'} href={tabHref('/scale/status')}>status</a>
	<a class:selected={selectedView === 'calibration'} href={tabHref('/scale/calibration')}>calibration</a>
</div>

<ScaleTab {selectedView} />

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
