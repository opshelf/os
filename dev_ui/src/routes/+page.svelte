<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch, getApiBase, isApiConnected, robotStatus, robotStatusError, robotStatusLastUpdate } from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	const apiBase = getApiBase();

	let deviceId = '';
	let lanIp = '';

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
				<td>light curtain</td>
				<td>
					{#if $robotStatus.robot_status?.curtain_blocked === null}
						disabled
					{:else if $robotStatus.robot_status?.curtain_blocked === true}
						blocked
					{:else if $robotStatus.robot_status?.curtain_blocked === false}
						clear
					{:else}
						unknown
					{/if}
				</td>
			</tr>
			<tr>
				<td>home required</td>
				<td>
					{#if $robotStatus.needs_home}
						yes{#if $robotStatus.needs_home_reason}
							({$robotStatus.needs_home_reason}){/if}
					{:else}
						no
					{/if}
				</td>
			</tr>
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
