<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { getSizerStatus, type SizerConfig, type SizerStatus } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let status: SizerStatus | null = $state(null);
	let statusError = $state('');
	let isLoading = $state(true);
	let isRefreshing = $state(false);

	async function fetchStatus(): Promise<void> {
		try {
			status = await getSizerStatus();
			statusError = '';
		} catch (err) {
			statusError = err instanceof Error ? err.message : 'failed to read sizer status';
		}
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		statusError = '';

		try {
			await fetchStatus();
		} finally {
			isRefreshing = false;
		}
	}

	function formatStatusValue(key: string, value: unknown): string {
		if (value === null || value === undefined) {
			return '-';
		}

		if (typeof value === 'boolean') {
			return value ? 'yes' : 'no';
		}

		if (Array.isArray(value)) {
			return value.length > 0 ? value.join(', ') : '-';
		}

		if (key === 'camera' && typeof value === 'object') {
			const camera = value as Partial<SizerConfig['camera']>;
			const device = camera.device ?? '-';
			const width = camera.width ?? '-';
			const height = camera.height ?? '-';
			const fps = camera.fps ?? '-';
			const flipped = camera.flip_horizontal === true ? ', flipped' : '';
			return `${device} (${width} × ${height} @ ${fps} fps${flipped})`;
		}

		if (typeof value === 'object') {
			return JSON.stringify(value);
		}

		return String(value);
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchStatus();
		} finally {
			isLoading = false;
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		initialLoad();
	});
</script>

{#if isLoading === true}
	<p>loading sizer data...</p>
{:else}
	<div class="scroll-x" style="margin-bottom: 1.5em">
		<table>
			<thead>
				<tr>
					<th colspan="2">sizer status</th>
				</tr>
			</thead>
			<tbody>
				{#if status !== null}
					{#each Object.entries(status) as [key, value]}
						<tr>
							<td>{key.replaceAll('_', ' ')}</td>
							<td>{formatStatusValue(key, value)}</td>
						</tr>
					{/each}
				{:else if statusError === ''}
					<tr>
						<td colspan="2">loading...</td>
					</tr>
				{/if}
			</tbody>
		</table>
	</div>

	{#if statusError !== ''}
		<ErrorMessage message={statusError} />
	{/if}

	<div class="action-buttons">
		<button type="button" onclick={didClickRefresh} disabled={isRefreshing === true}>refresh</button>
	</div>
{/if}

<style>
	.action-buttons {
		display: flex;
		gap: 1em;
		margin-bottom: 1.5em;
	}
</style>
