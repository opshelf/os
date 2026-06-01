<script lang="ts">
	import {
		controlCabinetLock,
		getCabinetStatus,
		type CabinetLockAction,
		type CabinetLockState
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let activeAction: CabinetLockAction | null = $state(null);
	let isRefreshing = $state(false);
	let error = $state('');
	let message = $state('');
	let lockState: CabinetLockState | null = $state(null);

	async function didRefreshStatus(): Promise<void> {
		isRefreshing = true;
		error = '';

		try {
			const response = await getCabinetStatus();
			lockState = response.locks;
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
			lockState = response.locks;
			message = action === 'lock' ? 'cabinet locked' : 'cabinet unlocked';
		} catch (err) {
			error = err instanceof Error ? err.message : `failed to ${action} cabinet`;
		} finally {
			activeAction = null;
		}
	}
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
</div>

{#if lockState !== null}
	<table>
		<tbody>
			<tr>
				<td>top lock engaged</td>
				<td>{lockState.top_locked ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>bottom lock engaged</td>
				<td>{lockState.bottom_locked ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>both locked</td>
				<td>{lockState.locked ? 'yes' : 'no'}</td>
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
