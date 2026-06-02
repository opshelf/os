<script lang="ts">
	import {
		controlCabinetRelease,
		getCabinetStatus,
		type CabinetDoorState,
		type CabinetLatchAction,
		type CabinetLatchState
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';
	import { onMount } from 'svelte';

	let activeAction: CabinetLatchAction | null = $state(null);
	let isRefreshing = $state(false);
	let error = $state('');
	let message = $state('');
	let doorState: CabinetDoorState | null = $state(null);
	let latchState: CabinetLatchState | null = $state(null);

	async function didRefreshStatus(): Promise<void> {
		isRefreshing = true;
		error = '';

		try {
			const response = await getCabinetStatus();
			doorState = response.doors;
			latchState = response.latches;
			message = 'cabinet status refreshed';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to refresh cabinet status';
		} finally {
			isRefreshing = false;
		}
	}

	async function didControlCabinetRelease(action: CabinetLatchAction): Promise<void> {
		activeAction = action;
		error = '';
		message = '';

		try {
			const response = await controlCabinetRelease(action);
			doorState = response.doors;
			latchState = response.latches;
			message = `cabinet ${action} command sent`;
		} catch (err) {
			error = err instanceof Error ? err.message : `failed to ${action} cabinet`;
		} finally {
			activeAction = null;
		}
	}

	onMount(() => {
		didRefreshStatus();
	});
</script>

<svelte:head>
	<title>cabinet</title>
</svelte:head>

<div class="actions">
	<button
		disabled={activeAction !== null || isRefreshing === true}
		onclick={() => didControlCabinetRelease('passive')}
	>
		PASSIVE
	</button>
	<button
		disabled={activeAction !== null || isRefreshing === true}
		onclick={() => didControlCabinetRelease('release')}
	>
		RELEASE
	</button>
	<button disabled={activeAction !== null || isRefreshing === true} onclick={didRefreshStatus}>
		REFRESH
	</button>
</div>

{#if doorState !== null}
	<table style="margin-top: 1.5em">
		<tbody>
			<tr>
				<td>top closed</td>
				<td>{doorState.top_closed ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>bottom closed</td>
				<td>{doorState.bottom_closed ? 'yes' : 'no'}</td>
			</tr>
		</tbody>
	</table>
{/if}

{#if latchState !== null}
	<table style="margin-top: 1.5em">
		<tbody>
			<tr>
				<td>release output</td>
				<td>{latchState.released ? 'energized' : 'passive'}</td>
			</tr>
			<tr>
				<td>top engaged sensor</td>
				<td>{latchState.top_engaged ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>bottom engaged sensor</td>
				<td>{latchState.bottom_engaged ? 'yes' : 'no'}</td>
			</tr>
		</tbody>
	</table>
{/if}

{#if message}
	<StatusMessage {message} />
{/if}

{#if error}
	<ErrorMessage message={error} />
{/if}

<style>
	.actions {
		display: flex;
		gap: 1em;
	}
</style>
