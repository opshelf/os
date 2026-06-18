<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getWifiStatus,
		scanWifiNetworks,
		connectWifi,
		type WifiStatus,
		type WifiNetwork
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let status: WifiStatus | null = null;
	let networks: WifiNetwork[] = [];
	let ssid = '';
	let password = '';
	let selectedSsid = '';
	let selectedSecurity = '';
	let passwordInput: HTMLInputElement | null = null;

	let isLoadingStatus = true;
	let isScanning = false;
	let isConnecting = false;
	let error = '';
	let message = '';

	const STATUS_REPOLL_DELAY_MS = 4000;

	function statusLabel(): string {
		if (status === null) {
			return 'unknown';
		}

		if (status.connected === true && status.ssid !== null) {
			const signal = status.signal !== null ? ` (${status.signal}%)` : '';
			const ip = status.ip !== null ? ` — ${status.ip}` : '';
			return `connected to ${status.ssid}${signal}${ip}`;
		}

		return 'not connected';
	}

	async function fetchStatus(): Promise<void> {
		try {
			status = await getWifiStatus();
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load wifi status';
		}
	}

	async function didClickRefresh(): Promise<void> {
		isScanning = true;
		error = '';

		try {
			const result = await scanWifiNetworks();
			networks = result.networks;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to scan networks';
		} finally {
			isScanning = false;
		}
	}

	function isSelectedOpenNetwork(): boolean {
		return selectedSsid !== '' && ssid === selectedSsid && selectedSecurity.toLowerCase() === 'open';
	}

	function didSelectNetwork(network: WifiNetwork): void {
		ssid = network.ssid;
		selectedSsid = network.ssid;
		selectedSecurity = network.security;
		message = '';
		error = '';

		if (passwordInput !== null) {
			passwordInput.focus();
		}
	}

	function didInputSsid(): void {
		if (ssid !== selectedSsid) {
			selectedSsid = '';
			selectedSecurity = '';
		}
	}

	async function didClickSave(): Promise<void> {
		if (ssid.trim() === '') {
			error = 'enter a network name';
			return;
		}

		if (password === '' && isSelectedOpenNetwork() === false) {
			error = 'enter a password';
			return;
		}

		isConnecting = true;
		error = '';
		message = '';

		try {
			const result = await connectWifi(ssid.trim(), password);
			message = result.message;
			password = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to connect';
		} finally {
			isConnecting = false;
			setTimeout(() => {
				void fetchStatus();
			}, STATUS_REPOLL_DELAY_MS);
		}
	}

	async function initialLoad(): Promise<void> {
		isLoadingStatus = true;

		try {
			await fetchStatus();
		} finally {
			isLoadingStatus = false;
		}

		await didClickRefresh();
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		void initialLoad();
	});
</script>

<div class="current">
	<span class="label">current</span>
	<span class="value">{isLoadingStatus === true ? 'loading...' : statusLabel()}</span>
	<button type="button" on:click={fetchStatus} disabled={isConnecting === true}>↻</button>
</div>

<div class="form">
	<label>
		network
		<input
			type="text"
			placeholder="ssid"
			bind:value={ssid}
			disabled={isConnecting === true}
			on:input={didInputSsid}
		/>
	</label>
	<label>
		password
		<input
			type="password"
			placeholder="password"
			bind:value={password}
			bind:this={passwordInput}
			disabled={isConnecting === true}
		/>
	</label>
	<button type="button" on:click={didClickSave} disabled={isConnecting === true}>
		{isConnecting === true ? 'connecting...' : 'save'}
	</button>
</div>

{#if isConnecting === true}
	<p class="hint">switching networks may briefly drop this connection; status will refresh shortly.</p>
{/if}

<table class="wifi-table">
	<thead>
		<tr>
			<th colspan="3">
				<div class="section-heading">
					<span>networks</span>
					<button type="button" on:click={didClickRefresh} disabled={isScanning === true}>
						{isScanning === true ? 'scanning...' : 'refresh'}
					</button>
				</div>
			</th>
		</tr>
		<tr>
			<th class="network-name">network</th>
			<th>signal</th>
			<th>security</th>
		</tr>
	</thead>
	<tbody>
		{#if networks.length === 0}
			<tr>
				<td colspan="3">{isScanning === true ? 'scanning...' : 'no networks found'}</td>
			</tr>
		{:else}
			{#each networks as network (network.ssid)}
				<tr
					class="network-row"
					class:in-use={network.in_use === true}
					on:click={() => didSelectNetwork(network)}
				>
					<td class="network-name">
						{network.ssid}
						{#if network.in_use === true}<span class="in-use-tag">connected</span>{/if}
					</td>
					<td>{network.signal}%</td>
					<td>{network.security}</td>
				</tr>
			{/each}
		{/if}
	</tbody>
</table>

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.current {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-bottom: 1.5rem;
	}

	.current .label {
		color: #666;
		text-transform: uppercase;
		font-size: 0.8em;
	}

	.current .value {
		font-weight: 600;
	}

	.form {
		display: flex;
		align-items: flex-end;
		gap: 1rem;
		margin-bottom: 1rem;
	}

	.form label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.8em;
		color: #666;
		text-transform: uppercase;
	}

	.form input {
		width: 200px;
	}

	.hint {
		color: #888;
		font-size: 0.85em;
		margin: 0 0 1rem;
	}

	.wifi-table {
		margin-top: 0.5rem;
	}

	.network-name {
		width: 260px;
	}

	.section-heading {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.network-row {
		cursor: pointer;
	}

	.network-row:hover {
		background: #f3f3f3;
	}

	.network-row.in-use {
		font-weight: 600;
	}

	.in-use-tag {
		margin-left: 0.5rem;
		color: green;
		font-size: 0.75em;
		text-transform: uppercase;
	}

	button {
		margin-top: 0;
	}
</style>
