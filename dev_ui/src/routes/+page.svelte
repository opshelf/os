<script lang="ts">
	import { onMount } from 'svelte';
	import {
		apiFetch,
		controlCabinetRelease,
		setBypassSafetyGuard,
		setBypassSafetyComponent,
		getApiBase,
		isApiConnected,
		robotInit,
		robotStatus,
		robotStatusLastUpdate,
		type SafetyComponent
	} from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import InitProgress from '$lib/components/InitProgress.svelte';
	import TrayRecovery from '$lib/components/TrayRecovery.svelte';

	const apiBase = getApiBase();
	const RESTART_BUTTON_RESET_MS = 15000;

	let deviceId = '';
	let lanIp = '';
	let isUnlockingCabinet = false;
	let isHomingRobot = false;
	let isUpdatingSafetyOverride = false;
	let safetyOverrideError = '';
	let homeOnInit = true;
	let isLoadingHomeOnInit = true;
	let isUpdatingHomeOnInit = false;
	let homeOnInitError = '';
	let isRestartingApi = false;
	let restartApiError = '';
	let componentUpdating: Record<SafetyComponent, boolean> = {
		light_curtain: false,
		cabinet_doors: false
	};
	let componentErrors: Record<SafetyComponent, string> = {
		light_curtain: '',
		cabinet_doors: ''
	};

	const safetyComponents: { key: SafetyComponent; label: string }[] = [
		{ key: 'light_curtain', label: 'bypass light curtain' },
		{ key: 'cabinet_doors', label: 'bypass cabinet sensors' }
	];

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

	function getBypassSafetyGuard(): boolean {
		const safety = $robotStatus?.robot_status?.safety;
		if (!safety || typeof safety !== 'object') {
			return false;
		}

		return (safety as Record<string, unknown>).bypassed === true;
	}

	async function didClickCabinetUnlock(): Promise<void> {
		isUnlockingCabinet = true;

		try {
			await controlCabinetRelease('release');
		} catch (err) {
			console.error('failed to unlock cabinet:', err);
		} finally {
			isUnlockingCabinet = false;
		}
	}

	async function didClickBypassSafetyGuard(): Promise<void> {
		const shouldBypassSafetyGuard = !getBypassSafetyGuard();
		isUpdatingSafetyOverride = true;
		safetyOverrideError = '';

		try {
			await setBypassSafetyGuard(shouldBypassSafetyGuard);
		} catch (err) {
			safetyOverrideError = err instanceof Error ? err.message : 'failed to update safety override';
		} finally {
			isUpdatingSafetyOverride = false;
		}
	}

	function getComponentBypass(component: SafetyComponent): boolean {
		const safety = $robotStatus?.robot_status?.safety;
		if (!safety || typeof safety !== 'object') {
			return false;
		}
		const componentBypasses = (safety as Record<string, unknown>).component_bypasses;
		if (!componentBypasses || typeof componentBypasses !== 'object') {
			return false;
		}
		return (componentBypasses as Record<string, unknown>)[component] === true;
	}

	async function didClickBypassComponent(component: SafetyComponent): Promise<void> {
		const shouldBypass = !getComponentBypass(component);
		componentUpdating = { ...componentUpdating, [component]: true };
		componentErrors = { ...componentErrors, [component]: '' };

		try {
			await setBypassSafetyComponent(component, shouldBypass);
		} catch (err) {
			const message = err instanceof Error ? err.message : 'failed to update safety override';
			componentErrors = { ...componentErrors, [component]: message };
		} finally {
			componentUpdating = { ...componentUpdating, [component]: false };
		}
	}

	async function didClickHome(): Promise<void> {
		isHomingRobot = true;

		try {
			await apiFetch('/robot/commands', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ command: 'home' })
			});
		} catch (err) {
			console.error('home command failed:', err);
		} finally {
			isHomingRobot = false;
		}
	}

	async function loadHomeOnInit(): Promise<void> {
		isLoadingHomeOnInit = true;
		homeOnInitError = '';

		try {
			const data = await apiFetch<{ home_on_init: boolean }>('/system/home-on-init');
			homeOnInit = data.home_on_init;
		} catch (err) {
			homeOnInitError = err instanceof Error ? err.message : 'failed to load homing setting';
		} finally {
			isLoadingHomeOnInit = false;
		}
	}

	async function didClickHomeOnInit(): Promise<void> {
		const shouldHomeOnInit = !homeOnInit;
		isUpdatingHomeOnInit = true;
		homeOnInitError = '';

		try {
			const data = await apiFetch<{ home_on_init: boolean }>('/system/home-on-init', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ home_on_init: shouldHomeOnInit })
			});
			homeOnInit = data.home_on_init;
		} catch (err) {
			homeOnInitError = err instanceof Error ? err.message : 'failed to update homing setting';
		} finally {
			isUpdatingHomeOnInit = false;
		}
	}

	async function didClickRestartApi(): Promise<void> {
		isRestartingApi = true;
		restartApiError = '';

		try {
			await apiFetch('/system/restart', {
				method: 'POST'
			});
			setTimeout(() => {
				isRestartingApi = false;
			}, RESTART_BUTTON_RESET_MS);
		} catch (err) {
			restartApiError = err instanceof Error ? err.message : 'failed to restart api';
			isRestartingApi = false;
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
		loadHomeOnInit();
	});
</script>

{#if $robotInit !== null && $robotInit.phase !== 'ready' && $robotInit.phase !== 'disabled'}
	<InitProgress init={$robotInit} />
{/if}

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
				<td>
					<div class="status-action-cell">
						<span>{$robotStatus.running ? 'yes' : 'no'}</span>
						<button disabled={isRestartingApi === true} onclick={didClickRestartApi}>RESTART</button
						>
					</div>
					{#if restartApiError !== ''}
						<ErrorMessage message={restartApiError} />
					{/if}
				</td>
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
						<button
							disabled={isUnlockingCabinet === true || getCabinetStatus() !== 'closed'}
							onclick={didClickCabinetUnlock}>UNLOCK</button
						>
					</div>
				</td>
			</tr>
			<tr>
				<td>home required</td>
				<td>
					<div class="status-action-cell">
						<span>
							{#if $robotStatus.needs_home}
								yes
							{:else}
								no
							{/if}
						</span>
						<button
							disabled={$robotStatus.needs_home !== true || isHomingRobot === true}
							onclick={didClickHome}>HOME</button
						>
					</div>
				</td>
			</tr>
			<TrayRecovery />
		</tbody>
	{/if}

	<thead>
		<tr>
			<th colspan="2">control loop override</th>
		</tr>
	</thead>
	<tbody>
		<tr>
			<td>homing on startup</td>
			<td>
				<div class="status-action-cell">
					<span
						>{isLoadingHomeOnInit === true
							? 'loading...'
							: homeOnInit
								? 'enabled'
								: 'disabled'}</span
					>
					<button
						disabled={isLoadingHomeOnInit === true || isUpdatingHomeOnInit === true}
						onclick={didClickHomeOnInit}
					>
						{homeOnInit ? 'DISABLE' : 'ENABLE'}
					</button>
				</div>
				{#if homeOnInitError !== ''}
					<ErrorMessage message={homeOnInitError} />
				{/if}
			</td>
		</tr>
		{#if $robotStatus}
			<tr>
				<td>bypass safety guard</td>
				<td>
					<div class="status-action-cell">
						<span class:enabled-danger={getBypassSafetyGuard() === true}>
							{getBypassSafetyGuard() ? 'enabled' : 'disabled'}
						</span>
						<button
							disabled={isUpdatingSafetyOverride === true}
							onclick={didClickBypassSafetyGuard}
						>
							{getBypassSafetyGuard() ? 'DISABLE' : 'ENABLE'}
						</button>
					</div>
					{#if safetyOverrideError !== ''}
						<ErrorMessage message={safetyOverrideError} />
					{/if}
				</td>
			</tr>
			{#each safetyComponents as component (component.key)}
				<tr>
					<td>{component.label}</td>
					<td>
						<div class="status-action-cell">
							<span class:enabled-danger={getComponentBypass(component.key) === true}>
								{getComponentBypass(component.key) ? 'enabled' : 'disabled'}
							</span>
							<button
								disabled={componentUpdating[component.key] === true}
								onclick={() => didClickBypassComponent(component.key)}
							>
								{getComponentBypass(component.key) ? 'DISABLE' : 'ENABLE'}
							</button>
						</div>
						{#if componentErrors[component.key] !== ''}
							<ErrorMessage message={componentErrors[component.key]} />
						{/if}
					</td>
				</tr>
			{/each}
		{/if}
	</tbody>

	{#if $robotStatus}
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

	.enabled-danger {
		color: #dc2626;
	}
</style>
