<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { scanWifiNetworks, connectWifi, type WifiNetwork } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let networks: WifiNetwork[] = [];
	let ssid = '';
	let password = '';
	let selectedSsid = '';
	let selectedSecurity = '';
	let passwordInput: HTMLInputElement | null = null;

	let isScanning = false;
	let isConnecting = false;
	let error = '';
	let message = '';

	const RESCAN_DELAY_MS = 4000;

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

	function didClickUse(network: WifiNetwork): void {
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
				void didClickRefresh();
			}, RESCAN_DELAY_MS);
		}
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		void didClickRefresh();
	});
</script>

<table class="connect-table">
	<thead>
		<tr>
			<th colspan="2">connect</th>
		</tr>
	</thead>
	<tbody>
		<tr>
			<td>
				<span class="field-label">network</span>
				<input
					type="text"
					placeholder="ssid"
					bind:value={ssid}
					disabled={isConnecting === true}
					on:input={didInputSsid}
				/>
			</td>
			<td>
				<span class="field-label">password</span>
				<input
					type="password"
					placeholder="password"
					bind:value={password}
					bind:this={passwordInput}
					disabled={isConnecting === true}
				/>
			</td>
		</tr>
		<tr>
			<td colspan="2">
				<div class="action-buttons">
					<button type="button" on:click={didClickSave} disabled={isConnecting === true}>
						{isConnecting === true ? 'connecting...' : 'save'}
					</button>
					<button type="button" on:click={didClickRefresh} disabled={isScanning === true}>
						{isScanning === true ? 'scanning...' : 'refresh'}
					</button>
				</div>
			</td>
		</tr>
	</tbody>
</table>

{#if isConnecting === true}
	<p class="hint">switching networks may briefly drop this connection; networks will refresh shortly.</p>
{/if}

<table class="networks-table">
	<thead>
		<tr>
			<th class="network-name">network</th>
			<th>signal</th>
			<th>actions</th>
		</tr>
	</thead>
	<tbody>
		{#if networks.length === 0}
			<tr>
				<td colspan="3">{isScanning === true ? 'scanning...' : 'no networks found'}</td>
			</tr>
		{:else}
			{#each networks as network (network.ssid)}
				<tr>
					<td class="network-name">{network.ssid}</td>
					<td>{network.signal}%</td>
					<td>
						{#if network.in_use === true}
							<span class="in-use">in use</span>
						{:else}
							<button type="button" on:click={() => didClickUse(network)}>use</button>
						{/if}
					</td>
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
	.connect-table {
		margin-bottom: 1.5em;
	}

	.field-label {
		margin-right: 0.5em;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.action-buttons button {
		margin-top: 0;
	}

	.hint {
		margin-bottom: 1.5em;
		text-transform: uppercase;
		color: oklch(0.5318 0.2399 256.98999);
	}

	.networks-table {
		min-width: 24em;
	}

	.network-name {
		white-space: nowrap;
	}

	.networks-table button {
		margin-top: 0;
	}

	.in-use {
		text-transform: uppercase;
		color: oklch(0.5318 0.2399 256.98999);
	}
</style>
