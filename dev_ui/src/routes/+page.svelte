<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { apiFetch, getApiBase, isApiConnected, robotStatus, robotStatusError, robotStatusLastUpdate } from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';


	type StorageCell = {
		shelf_idx: number;
		cell_idx: number;
		capacity: string;
		status: string;
	};

	const apiBase = getApiBase();
	const hostname = browser ? window.location.hostname : '';


	let lanIp = '';

	let storageCells: StorageCell[] = [];
	let storageError = '';
	let storageLoading = true;


	async function loadStorage(): Promise<void> {
		storageLoading = true;
		storageError = '';
		try {
			const data = await apiFetch<StorageCell[]>('/storage/locations');
			if (Array.isArray(data)) {
				storageCells = data;
			} else {
				storageError = 'unexpected storage payload format';
			}
		} catch (error) {
			storageError = error instanceof Error ? error.message : 'failed to load storage locations';
		} finally {
			storageLoading = false;
		}
	}

	function storageSummary(): Array<{ status: string; count: number }> {
		const summary = new Map<string, number>();
		for (const cell of storageCells) {
			const key = cell.status ?? 'unknown';
			summary.set(key, (summary.get(key) ?? 0) + 1);
		}
		return Array.from(summary.entries()).map(([status, count]) => ({ status, count }));
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
			const data = await apiFetch<{ lan_ip: string | null }>('/system/info');

			lanIp = data.lan_ip ?? '';
		} catch {
			lanIp = '';
		}
	}

	onMount(() => {
		loadStorage();
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
			<td>{hostname}</td>
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
				<td>queued commands</td>
				<td>{$robotStatus.queue_length ?? 0}</td>
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
				<td>door</td>
				<td>
					{#if $robotStatus.robot_status?.door_open === null}
						unknown
					{:else if $robotStatus.robot_status?.door_open === true}
						open
					{:else}
						closed
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

	<thead>
		<tr>
			<th colspan="2">storage summary</th>
		</tr>
	</thead>
	<tbody>
		{#if storageLoading}
			<tr>
				<td colspan="2">loading storage data...</td>
			</tr>
		{:else if storageError}
			<tr>
				<td colspan="2"><ErrorMessage message={storageError} /></td>
			</tr>
		{:else}
			<tr>
				<td>total cells</td>
				<td>{storageCells.length}</td>
			</tr>
		{/if}
	</tbody>

	{#if !storageLoading && !storageError && storageCells.length > 0}
		<thead>
			<tr>
				<th>cells by status</th>
				<th>count</th>
			</tr>
		</thead>
		<tbody>
			{#each storageSummary() as row}
				<tr>
					<td>{row.status}</td>
					<td>{row.count}</td>
				</tr>
			{/each}
		</tbody>

		<thead>
			<tr>
				<th>shelf</th>
				<th>cell</th>
				<th>capacity</th>
				<th>status</th>
			</tr>
		</thead>
		<tbody>
			{#each storageCells.slice(0, 10) as cell}
				<tr>
					<td>{cell.shelf_idx}</td>
					<td>{cell.cell_idx}</td>
					<td>{cell.capacity}</td>
					<td>{cell.status}</td>
				</tr>
			{/each}
		</tbody>
	{/if}
</table>
