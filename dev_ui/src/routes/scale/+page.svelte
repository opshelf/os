<script lang="ts">
	import { page } from '$app/state';
	import ScaleTab from '$lib/components/scale/ScaleTab.svelte';
	import { devicePath } from '$lib/nav';

	function tabHref(path: string): string {
		return `${devicePath(path)}${page.url.search}`;
	}
</script>

<svelte:head>
	<title>Scale • Openshelf</title>
</svelte:head>

<div class="toggle-group">
	<a class:selected={true} href={tabHref('/scale/status')}>status</a>
	<a href={tabHref('/scale/calibration')}>calibration</a>
</div>

<ScaleTab selectedView="status" />

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
