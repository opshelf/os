<script lang="ts">
	import {
		controlCabinetLock,
		getCabinetStatus,
		type CabinetDoorState,
		type CabinetLockAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';
	import { onMount } from 'svelte';

	let activeAction: CabinetLockAction | null = $state(null);
	let isRefreshing = $state(false);
	let error = $state('');
	let message = $state('');
	let doorState: CabinetDoorState | null = $state(null);

	async function didRefreshStatus(): Promise<void> {
		isRefreshing = true;
		error = '';

		try {
			const response = await getCabinetStatus();
			doorState = response.doors;
			message = 'cabinet status refreshed';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to refresh cabinet status';
		} finally {
			isRefreshing = false;
		}
	}

	async function didControlCabinetLock(action: CabinetLockAction): Promise<void> {
		activeAction = action;
		error = '';
		message = '';

		try {
			const response = await controlCabinetLock(action);
			doorState = response.doors;
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
		onclick={() => didControlCabinetLock('lock')}
	>
		LOCK
	</button>
	<button
		disabled={activeAction !== null || isRefreshing === true}
		onclick={() => didControlCabinetLock('unlock')}
	>
		UNLOCK
	</button>
	<button disabled={activeAction !== null || isRefreshing === true} onclick={didRefreshStatus}>
		REFRESH
	</button>
</div>

{#if doorState !== null}
	<table>
		<tbody>
			<tr>
				<td>top closed</td>
				<td>{doorState.top_closed ? 'no' : 'yes'}</td>
			</tr>
			<tr>
				<td>bottom closed</td>
				<td>{doorState.bottom_closed ? 'no' : 'yes'}</td>
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
		margin: 1em 0;
	}
</style>
