<script lang="ts">
	import { browser } from '$app/environment';
	import { onDestroy, onMount } from 'svelte';
	import { apiFetch, buildApiUrl, getApiBase } from '$lib';

	type WorkerStatusPayload = {
		robot_status?: string | null;
		running?: boolean;
		current_command?: Record<string, unknown> | null;
		queue_length?: number;
		error_queue?: Array<Record<string, unknown>>;
		queued_operations?: Array<{ command?: Record<string, unknown>; timestamp?: string }>;
	};

	type StorageCell = {
		shelf_idx: number;
		cell_idx: number;
		capacity: string;
		status: string;
	};

	const apiBase = getApiBase();
	const hostname = browser ? window.location.hostname : '';

	let status: WorkerStatusPayload | null = null;
	let statusError = '';
	let statusConnected = false;
	let lastStatusUpdate = '';

	let storageCells: StorageCell[] = [];
	let storageError = '';
	let storageLoading = true;

	let eventSource: EventSource | null = null;

	function handleStatusMessage(raw: string): void {
		try {
			const payload = JSON.parse(raw) as WorkerStatusPayload;
			status = payload;
			statusConnected = true;
			statusError = '';
			lastStatusUpdate = new Date().toLocaleString();
		} catch (error) {
			statusError = 'Failed to parse robot status payload';
		}
	}

	async function loadStorage(): Promise<void> {
		storageLoading = true;
		storageError = '';
		try {
			const data = await apiFetch<StorageCell[]>('/storage/locations');
			if (Array.isArray(data)) {
				storageCells = data;
			} else {
				storageError = 'Unexpected storage payload format';
			}
		} catch (error) {
			storageError = error instanceof Error ? error.message : 'Failed to load storage locations';
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

	function formatCommand(command: Record<string, unknown> | null | undefined): Array<{ field: string; value: string }> {
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

	onMount(() => {
		loadStorage();
		if (!browser) {
			return;
		}

		const source = new EventSource(buildApiUrl('/robot/status'));
		eventSource = source;

		source.onopen = () => {
			statusConnected = true;
			statusError = '';
		};

		source.onmessage = (event: MessageEvent<string>) => {
			handleStatusMessage(event.data);
		};

		source.onerror = () => {
			statusConnected = false;
			statusError = 'Lost connection to robot status stream';
		};

		return () => {
			source.close();
		};
	});

	onDestroy(() => {
		eventSource?.close();
	});
</script>

<h1>Robot Status</h1>

<table>
	<tbody>
		<tr>
			<td>API Base URL</td>
			<td>{apiBase}</td>
		</tr>
		<tr>
			<td>UI Hostname</td>
			<td>{hostname}</td>
		</tr>
		<tr>
			<td>Status Stream</td>
			<td>{statusConnected ? 'connected' : 'disconnected'}</td>
		</tr>
		<tr>
			<td>Last Update</td>
			<td>{lastStatusUpdate || 'waiting for first event'}</td>
		</tr>
	</tbody>
</table>

{#if statusError}
	<p>{statusError}</p>
{/if}

{#if status}
	<h2>Current Robot State</h2>
	<table>
		<tbody>
			<tr>
				<td>Robot Status</td>
				<td>{status.robot_status ?? 'unknown'}</td>
			</tr>
			<tr>
				<td>Worker Running</td>
				<td>{status.running ? 'yes' : 'no'}</td>
			</tr>
			<tr>
				<td>Queued Commands</td>
				<td>{status.queue_length ?? 0}</td>
			</tr>
		</tbody>
	</table>

	{#if status.current_command}
		<h3>Current Command</h3>
		<table>
			<tbody>
				{#each formatCommand(status.current_command) as entry}
					<tr>
						<td>{entry.field}</td>
						<td>{entry.value}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if status.error_queue && status.error_queue.length > 0}
		<h3>Recent Errors</h3>
		<table>
			<thead>
				<tr>
					<th>Type</th>
					<th>Message</th>
				</tr>
			</thead>
			<tbody>
				{#each status.error_queue as error}
					<tr>
						<td>{formatValue(error.error_type)}</td>
						<td>{formatValue(error.message)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
{/if}

<h2>Storage Summary</h2>

{#if storageLoading}
	<p>Loading storage data...</p>
{:else}
	{#if storageError}
		<p>{storageError}</p>
	{:else}
		<table>
			<tbody>
				<tr>
					<td>Total Cells</td>
					<td>{storageCells.length}</td>
				</tr>
			</tbody>
		</table>

		{#if storageCells.length > 0}
			<h3>Cells by Status</h3>
			<table>
				<thead>
					<tr>
						<th>Status</th>
						<th>Count</th>
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
			</table>

			<h3>First Ten Cells</h3>
			<table>
				<thead>
					<tr>
						<th>Shelf</th>
						<th>Cell</th>
						<th>Capacity</th>
						<th>Status</th>
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
			</table>
		{/if}
	{/if}
{/if}
