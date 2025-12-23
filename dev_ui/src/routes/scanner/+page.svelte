<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount, onDestroy } from 'svelte';
	import { getScannerStatus, sendScannerScan, getApiBase, type ScannerStatus } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let status: ScannerStatus | null = null;
	let latestReading = '';
	let statusError = '';
	let scanError = '';
	let message = '';
	let isLoading = true;
	let isRefreshing = false;
	let isSending = false;
	let simulatedCode = '';
	let eventSource: EventSource | null = null;

	async function fetchStatus(): Promise<void> {
		try {
			status = await getScannerStatus();
			statusError = '';
		} catch (err) {
			statusError = err instanceof Error ? err.message : 'failed to read scanner status';
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

	async function didClickSend(): Promise<void> {
		if (simulatedCode.length === 0) {
			scanError = 'enter a code to send';
			return;
		}

		isSending = true;
		scanError = '';
		message = '';

		try {
			await sendScannerScan(simulatedCode);
			message = 'scan sent';
		} catch (err) {
			scanError = err instanceof Error ? err.message : 'failed to send scan';
		} finally {
			isSending = false;
		}
	}

	function generateRandomUpca(): string {
		const digits: number[] = [];

		for (let i = 0; i < 11; i++) {
			digits.push(Math.floor(Math.random() * 10));
		}

		let oddSum = 0;
		let evenSum = 0;

		for (let i = 0; i < 11; i++) {
			if (i % 2 === 0) {
				oddSum += digits[i];
			} else {
				evenSum += digits[i];
			}
		}

		const checkDigit = (10 - ((oddSum * 3 + evenSum) % 10)) % 10;
		digits.push(checkDigit);

		return digits.join('');
	}

	function didClickGenerate(): void {
		simulatedCode = generateRandomUpca();
	}

	function connectEventSource(): void {
		if (eventSource !== null) {
			return;
		}

		const url = `${getApiBase()}/robot/events`;
		eventSource = new EventSource(url);

		eventSource.onmessage = function (event) {
			try {
				const data = JSON.parse(event.data);
				if (data.kind === 'label_scanned' && typeof data.payload === 'string') {
					latestReading = data.payload;
				}
			} catch {
				// ignore parse errors
			}
		};

		eventSource.onerror = function () {
			if (eventSource !== null) {
				eventSource.close();
				eventSource = null;
			}
			setTimeout(connectEventSource, 2000);
		};
	}

	function disconnectEventSource(): void {
		if (eventSource !== null) {
			eventSource.close();
			eventSource = null;
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchStatus();
		} finally {
			isLoading = false;
		}

		connectEventSource();
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		initialLoad();
	});

	onDestroy(function () {
		disconnectEventSource();
	});
</script>

{#if isLoading === true}
	<p>loading scanner data...</p>
{:else}
	<table>
		<thead>
			<tr>
				<th colspan="2">scanner status</th>
			</tr>
		</thead>
		<tbody>
			{#if status !== null}
				<tr>
					<td>enabled</td>
					<td
						class:status-ok={status.enabled === true}
						class:status-error={status.enabled === false}
					>
						{status.enabled ? 'yes' : 'no'}
					</td>
				</tr>
				<tr>
					<td>connected</td>
					<td
						class:status-ok={status.connected === true}
						class:status-error={status.connected === false}
					>
						{status.connected ? 'yes' : 'no'}
					</td>
				</tr>
			{:else if statusError === ''}
				<tr>
					<td colspan="2">loading...</td>
				</tr>
			{/if}
			{#if statusError !== ''}
				<tr>
					<td colspan="2"><ErrorMessage message={statusError} /></td>
				</tr>
			{/if}
		</tbody>

		<thead>
			<tr>
				<th colspan="2">latest reading</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td colspan="2" class="reading">{latestReading !== '' ? latestReading : '-'}</td>
			</tr>
		</tbody>

		<thead>
			<tr>
				<th colspan="2">simulated scan</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>code</td>
				<td>
					<input type="text" bind:value={simulatedCode} placeholder="012345678901" />
					<button type="button" class="random-button" on:click={didClickGenerate}>random</button>
				</td>
			</tr>
		</tbody>
	</table>

	<div class="action-buttons">
		<button type="button" on:click={didClickRefresh} disabled={isRefreshing === true}>
			refresh
		</button>
		<button
			type="button"
			on:click={didClickSend}
			disabled={isSending === true || simulatedCode.length === 0}
		>
			send scan
		</button>
	</div>

	{#if message !== ''}
		<div class="message">{message}</div>
	{/if}

	{#if scanError !== ''}
		<ErrorMessage message={scanError} />
	{/if}
{/if}

<style>
	.reading {
		font-family: monospace;
		font-size: 1.2em;
	}

	input[type='text'] {
		width: 12em;
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	.random-button {
		margin-top: 0;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1em;
	}

	.message {
		margin-top: 1em;
	}
</style>
