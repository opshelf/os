<script lang="ts">
	import { onMount } from 'svelte';
	import {
		apiFetch,
		controlCabinetRelease,
		getApiBase,
		isApiConnected,
		robotStatus,
		robotStatusError,
		robotStatusLastUpdate
	} from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	const apiBase = getApiBase();

	let deviceId = '';
	let lanIp = '';
	let isUnlockingCabinet = false;
	let cabinetActionError = '';
	let cabinetActionMessage = '';
	let isHomingRobot = false;
	let homeActionError = '';
	let homeActionMessage = '';

	function getLightCurtainBlocked(): unknown {
		const safety = $robotStatus?.robot_status?.safety;
		if (!safety || typeof safety !== 'object') {
			return undefined;
		}
		const sources = (safety as Record<string, unknown>).sources;
		if (!sources || typeof sources !== 'object') {
			return undefined;
		}
		const lightCurtain = (sources as Record<string, unknown>).light_curtain;
		if (!lightCurtain || typeof lightCurtain !== 'object') {
			return undefined;
		}
		return (lightCurtain as Record<string, unknown>).blocked;
	}

	function getIoBoxOccupied(): boolean {
		const scale = $robotStatus?.robot_status?.scale;
		if (!scale || typeof scale !== 'object') {
			return false;
		}
		return (scale as Record<string, unknown>).occupied === true;
	}

	function getCabinetStatus(): 'closed' | 'opened' | 'unknown' {
		const cabinetDoors = $robotStatus?.robot_status?.cabinet_doors;
		if (!cabinetDoors || typeof cabinetDoors !== 'object') {
			return 'unknown';
		}

		const state = cabinetDoors as Record<string, unknown>;
		if (typeof state.top_closed !== 'boolean' || typeof state.bottom_closed !== 'boolean') {
			return 'unknown';
		}

		return state.top_closed === true && state.bottom_closed === true ? 'closed' : 'opened';
	}

	async function didClickCabinetUnlock(): Promise<void> {
		isUnlockingCabinet = true;
		cabinetActionError = '';
		cabinetActionMessage = '';

		try {
			await controlCabinetRelease('release');
			cabinetActionMessage = 'cabinet unlock pulse complete';
		} catch (err) {
			cabinetActionError = err instanceof Error ? err.message : 'failed to unlock cabinet';
		} finally {
			isUnlockingCabinet = false;
		}
	}

	async function didClickHome(): Promise<void> {
		isHomingRobot = true;
		homeActionError = '';
		homeActionMessage = '';

		try {
			await apiFetch('/robot/commands', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ command: 'home' })
			});
			homeActionMessage = 'home command sent';
		} catch (err) {
			homeActionError = err instanceof Error ? err.message : 'home command failed';
		} finally {
			isHomingRobot = false;
		}
	}

	function formatCommand(
		command: Record<string, unknown> | null | undefined
	): Array<{ field: string; value: string }> {
		if (!command || typeof command !== 'object') {
			return [];
		}
		const entries: Array<{ field: string; value: string }> = [];
		for (const [key, value] of Object.entries(command)) {
			if (value === null || value === undefined) {
				continue;
			}
			if (typeof value === 'object' && !Array.isArray(value)) {
				for (const [innerKey, innerValue] of Object.entries(value)) {
					entries.push({ field: `${key}.${innerKey}`, value: formatValue(innerValue) });
				}
			} else {
				entries.push({ field: key, value: formatValue(value) });
			}
		}
		return entries;
	}

	function formatValue(value: unknown): string {
		if (typeof value === 'string') {
			return value;
		}
		if (typeof value === 'number' || typeof value === 'boolean') {
			return String(value);
		}
		return JSON.stringify(value);
	}

	async function loadSystemInfo(): Promise<void> {
		try {
			const data = await apiFetch<{ device_id: string; lan_ip: string | null }>('/system/info');

			deviceId = data.device_id;
			lanIp = data.lan_ip ?? '';
		} catch {
			deviceId = '';
			lanIp = '';
		}
	}

	onMount(() => {
		loadSystemInfo();
	});
</script>

<table>
	<thead>
		<tr>
			<th colspan="2">system information</th>
		</tr>
	</thead>
	<tbody>
		<tr>
			<td>api base url</td>
			<td>{apiBase}</td>
		</tr>
		<tr>
			<td>device id</td>
			<td>{deviceId || 'loading...'}</td>
		</tr>
		<tr>
			<td>lan ip</td>
			<td>{lanIp || 'loading...'}</td>
		</tr>
		<tr>
			<td>stream status</td>
			<td>{$isApiConnected ? 'connected' : 'disconnected'}</td>
		</tr>
		<tr>
			<td>last update</td>
			<td>{$robotStatusLastUpdate || 'waiting for first event'}</td>
		</tr>
		{#if $robotStatusError}
			<tr>
				<td colspan="2"><ErrorMessage message={$robotStatusError} /></td>
			</tr>
		{/if}
	</tbody>

	{#if $robotStatus}
		<thead>
			<tr>
				<th colspan="2">current robot state</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>worker running</td>
				<td>{$robotStatus.running ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>io box</td>
				<td>{getIoBoxOccupied() ? 'occupied' : 'empty'}</td>
			</tr>
			<tr>
				<td>light curtain</td>
				<td>
					{#if getLightCurtainBlocked() === null}
						disabled
					{:else if getLightCurtainBlocked() === true}
						blocked
					{:else if getLightCurtainBlocked() === false}
						clear
					{:else}
						unknown
					{/if}
				</td>
			</tr>
			<tr>
				<td>cabinet</td>
				<td>
					<div class="status-action-cell">
						<span>{getCabinetStatus()}</span>
						<button disabled={isUnlockingCabinet === true || getCabinetStatus() === 'opened'} onclick={didClickCabinetUnlock}>UNLOCK</button>
					</div>
				</td>
			</tr>
			{#if cabinetActionMessage}
				<tr>
					<td colspan="2"><StatusMessage message={cabinetActionMessage} /></td>
				</tr>
			{/if}
			{#if cabinetActionError}
				<tr>
					<td colspan="2"><ErrorMessage message={cabinetActionError} /></td>
				</tr>
			{/if}
			<tr>
				<td>home required</td>
				<td>
					<div class="status-action-cell">
						<span>
							{#if $robotStatus.needs_home}
								yes{#if $robotStatus.needs_home_reason}
									({$robotStatus.needs_home_reason}){/if}
							{:else}
								no
							{/if}
						</span>
						<button
							disabled={$robotStatus.needs_home !== true || isHomingRobot === true}
							onclick={didClickHome}>HOME</button>
					</div>
				</td>
			</tr>
			{#if homeActionMessage}
				<tr>
					<td colspan="2"><StatusMessage message={homeActionMessage} /></td>
				</tr>
			{/if}
			{#if homeActionError}
				<tr>
					<td colspan="2"><ErrorMessage message={homeActionError} /></td>
				</tr>
			{/if}
		</tbody>

		{#if $robotStatus.current_command}
			<thead>
				<tr>
					<th colspan="2">current command</th>
				</tr>
			</thead>
			<tbody>
				{#each formatCommand($robotStatus.current_command) as entry}
					<tr>
						<td>{entry.field}</td>
						<td>{entry.value}</td>
					</tr>
				{/each}
			</tbody>
		{/if}

		{#if $robotStatus.error_queue && $robotStatus.error_queue.length > 0}
			<thead>
				<tr>
					<th>recent errors - type</th>
					<th>message</th>
				</tr>
			</thead>
			<tbody>
				{#each $robotStatus.error_queue as error}
					<tr>
						<td>{formatValue(error.error_type)}</td>
						<td>{formatValue(error.message)}</td>
					</tr>
				{/each}
			</tbody>
		{/if}
	{/if}

</table>

<style>
	.status-action-cell {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1em;
	}
</style>
