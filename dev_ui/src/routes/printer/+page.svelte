<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getPrinterConfig,
		getPrinterStatus,
		printPrinterLabel,
		setPrinterConfig,
		type PrinterStatus
	} from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let status: PrinterStatus | null = null;
	let feedDots = 18;
	let code = '';
	let statusError = '';
	let configError = '';
	let printError = '';
	let message = '';
	let isLoading = true;
	let isRefreshing = false;
	let isSaving = false;
	let isPrinting = false;

	async function fetchStatus(): Promise<void> {
		try {
			status = await getPrinterStatus();
			statusError = '';
		} catch (err) {
			statusError = err instanceof Error ? err.message : 'failed to read printer status';
		}
	}

	async function fetchConfig(): Promise<void> {
		try {
			const config = await getPrinterConfig();
			feedDots = config.feed_dots;
			configError = '';
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to read printer config';
		}
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		await Promise.all([fetchStatus(), fetchConfig()]);
		isRefreshing = false;
	}

	async function didClickSave(): Promise<void> {
		if (Number.isInteger(feedDots) === false || feedDots < 0 || feedDots > 128) {
			configError = 'feed dots must be a whole number from 0 to 128';
			return;
		}

		isSaving = true;
		configError = '';
		message = '';

		try {
			const config = await setPrinterConfig({ feed_dots: feedDots });
			feedDots = config.feed_dots;
			message = `feed set to ${feedDots} dots`;
		} catch (err) {
			configError = err instanceof Error ? err.message : 'failed to save printer config';
		} finally {
			isSaving = false;
		}
	}

	async function didClickPrint(): Promise<void> {
		if (code.length === 0) {
			printError = 'enter a code to print';
			return;
		}

		isPrinting = true;
		printError = '';
		message = '';

		try {
			const result = await printPrinterLabel(code);
			if (result.printed === false) {
				printError = 'printer is disabled; label was not printed';
			} else {
				message = `printed ${result.barcode}`;
			}
		} catch (err) {
			printError = err instanceof Error ? err.message : 'failed to print label';
		} finally {
			isPrinting = false;
			await fetchStatus();
		}
	}

	function generateRandomBarcode(): string {
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
		code = generateRandomBarcode();
		printError = '';
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;
		await Promise.all([fetchStatus(), fetchConfig()]);
		isLoading = false;
	}

	onMount(() => {
		if (browser === true) {
			void initialLoad();
		}
	});
</script>

<svelte:head>
	<title>Printer • Openshelf</title>
</svelte:head>

{#if isLoading === true}
	<p>loading printer data...</p>
{:else}
	<div class="scroll-x">
		<table>
			<thead>
				<tr>
					<th colspan="2">printer status</th>
				</tr>
			</thead>
			<tbody>
				{#if status !== null}
					<tr>
						<td>enabled</td>
						<td class:status-ok={status.enabled} class:status-error={!status.enabled}>
							{status.enabled ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>connected</td>
						<td
							class:status-ok={status.connected || status.simulated}
							class:status-error={!status.connected && !status.simulated}
						>
							{status.simulated ? 'simulated' : status.connected ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>healthy</td>
						<td class:status-ok={status.healthy} class:status-error={!status.healthy}>
							{status.healthy ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>paper</td>
						<td
							class:status-ok={!status.paper_out && !status.paper_near_end}
							class:status-warning={status.paper_near_end && !status.paper_out}
							class:status-error={status.paper_out}
						>
							{status.paper_out ? 'out' : status.paper_near_end ? 'near end' : 'ok'}
						</td>
					</tr>
					<tr>
						<td>cover</td>
						<td class:status-ok={!status.cover_open} class:status-error={status.cover_open}>
							{status.cover_open ? 'open' : 'closed'}
						</td>
					</tr>
					<tr>
						<td>error</td>
						<td class:status-error={status.error !== null}>{status.error ?? '-'}</td>
					</tr>
				{/if}
			</tbody>

			<thead>
				<tr>
					<th colspan="2">feed calibration</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td><label for="feed-dots">feed dots</label></td>
					<td>
						<input id="feed-dots" type="number" min="0" max="128" step="1" bind:value={feedDots} />
					</td>
				</tr>
			</tbody>

			<thead>
				<tr>
					<th colspan="2">test label</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td><label for="print-code">code</label></td>
					<td>
						<input id="print-code" type="text" bind:value={code} placeholder="012345678901" />
						<button type="button" class="inline-button" on:click={didClickGenerate}>random</button>
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<div class="action-buttons">
		<button type="button" on:click={didClickRefresh} disabled={isRefreshing}>refresh</button>
		<button type="button" on:click={didClickSave} disabled={isSaving}>save feed</button>
		<button type="button" on:click={didClickPrint} disabled={isPrinting || code === ''}>
			print label
		</button>
	</div>

	{#if statusError !== ''}
		<ErrorMessage message={statusError} />
	{/if}

	{#if configError !== ''}
		<ErrorMessage message={configError} />
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if printError !== ''}
		<ErrorMessage message={printError} />
	{/if}
{/if}

<style>
	input {
		width: 12em;
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	input[type='number'] {
		width: 6em;
	}

	.inline-button {
		margin-top: 0;
	}

	.action-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 1.5em;
		margin-top: 1.5em;
	}

	.status-ok {
		color: #007a00;
	}

	.status-warning {
		color: #9a6500;
	}

	.status-error {
		color: #c40000;
	}
</style>
