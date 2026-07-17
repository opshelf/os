<script lang="ts">
	import { page } from '$app/state';
	import SizerStatusTab from '$lib/components/sizer/SizerStatusTab.svelte';
	import { devicePath } from '$lib/nav';

	function tabHref(path: string): string {
		return `${devicePath(path)}${page.url.search}`;
	}
</script>

<div class="toggle-group">
	<a class:selected={true} href={tabHref('/sizer/status')}>status</a>
	<a href={tabHref('/sizer/measurement')}>measurement</a>
	<a href={tabHref('/sizer/calibration')}>calibration</a>
</div>

<SizerStatusTab />

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
