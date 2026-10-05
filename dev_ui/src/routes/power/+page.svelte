<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { getPowerRails, powerCycleRail, type PowerRail } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	const STATE_POLL_INTERVAL_MS = 2000;

	let rails: PowerRail[] = [];
	let isLoading = true;
	let cyclingRail: string | null = null;
	let error = '';
	let loadError = '';
	let message = '';

	function equipmentLabel(rail: PowerRail): string {
		const equipment = [...rail.axes];

		if (rail.gripper === true) {
			equipment.push('gripper');
		}

		if (equipment.length === 0) {
			return rail.supplies;
		}

		return equipment.join(', ');
	}

	function powerLabel(powered: boolean | null): string {
		if (powered === null) {
			return 'UNKNOWN';
		}

		return powered === true ? 'ON' : 'OFF';
	}

	async function fetchRails(): Promise<void> {
		try {
			const data = await getPowerRails();
			rails = data.rails;
			loadError = '';
		} catch (err) {
			// Kept separate from `error` so the refresh after a cycle cannot
			// wipe out that cycle's failure message.
			loadError = err instanceof Error ? err.message : 'failed to load power rails';
		}
	}

	async function didClickCycle(rail: string): Promise<void> {
		cyclingRail = rail;
		error = '';
		message = '';

		try {
			const result = await powerCycleRail(rail);
			message =
				result.axes_needing_home.length > 0
					? `${rail} cycled, home required: ${result.axes_needing_home.join(', ')}`
					: `${rail} cycled`;
		} catch (err) {
			error = err instanceof Error ? err.message : `power cycle failed for ${rail}`;
		} finally {
			cyclingRail = null;
			await fetchRails();
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		void fetchRails().finally(() => {
			isLoading = false;
		});

		const interval = setInterval(() => {
			if (cyclingRail === null && document.visibilityState === 'visible') {
				void fetchRails();
			}
		}, STATE_POLL_INTERVAL_MS);

		return () => clearInterval(interval);
	});
</script>

{#if isLoading === true}
	<p>loading power rails...</p>
{:else}
	<table class="power-table">
		<thead>
			<tr>
				<th class="rail-column">rail</th>
				<th>equipment</th>
				<th class="status-column">power</th>
				<th class="actions-column">actions</th>
			</tr>
		</thead>
		<tbody>
			{#each rails as rail (rail.rail)}
				<tr>
					<td class="rail-column">{rail.rail}</td>
					<td>{equipmentLabel(rail)}</td>
					<td class="status-column">
						<span
							class:power-on={rail.powered === true}
							class:power-off={rail.powered === false}
							class:power-unknown={rail.powered === null}
						>
							{powerLabel(rail.powered)}
						</span>
					</td>
					<td class="actions-column">
						<button
							type="button"
							on:click={() => didClickCycle(rail.rail)}
							disabled={cyclingRail !== null}
						>
							{cyclingRail === rail.rail ? 'CYCLING...' : 'CYCLE'}
						</button>
					</td>
				</tr>
			{/each}
		</tbody>
	</table>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}

	{#if loadError !== ''}
		<ErrorMessage message={loadError} />
	{/if}
{/if}

<style>
	.rail-column,
	.status-column,
	.actions-column {
		white-space: nowrap;
	}

	button {
		margin-top: 0;
	}

	.power-on,
	.power-off,
	.power-unknown {
		color: black;
	}
</style>
