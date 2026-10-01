<script lang="ts">
	import { onMount } from 'svelte';
	import {
		apiFetch,
		acknowledgeSafety,
		ApiError,
		controlCabinetRelease,
		setBypassSafetyGuard,
		setBypassSafetyComponent,
		robotInit,
		robotStatus,
		type SafetyComponent
	} from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import InitProgress from '$lib/components/InitProgress.svelte';

	const RESTART_BUTTON_RESET_MS = 15000;

	let deviceId = '';
	let deviceAlias = '';
	let operatingMode = '';
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
	let isAcknowledgingSafety = false;
	let safetyAckError = '';
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

	function getSafetyAwaitingAck(): boolean {
		const safety = $robotStatus?.robot_status?.safety;
		if (!safety || typeof safety !== 'object') {
			return false;
		}
		return (safety as Record<string, unknown>).awaiting_ack === true;
	}

	// The server decides whether an acknowledgement would clear every active
	// source, including sources only an acknowledgement can clear (the scale
	// weight-change guard), so the button stays correct with or without the
	// motion guard.
	function getSafetyCanAcknowledge(): boolean {
		const safety = $robotStatus?.robot_status?.safety;
		if (!safety || typeof safety !== 'object') {
			return false;
		}
		return (safety as Record<string, unknown>).can_acknowledge === true;
	}

	async function didClickAcknowledgeSafety(): Promise<void> {
		isAcknowledgingSafety = true;
		safetyAckError = '';

		try {
			await acknowledgeSafety();
		} catch (err) {
			if (err instanceof ApiError && err.status === 409 && err.detail?.active_sources?.length) {
				safetyAckError = `cannot acknowledge: ${err.detail.active_sources.join(', ')}`;
			} else {
				safetyAckError = err instanceof Error ? err.message : 'acknowledge failed';
			}
		} finally {
			isAcknowledgingSafety = false;
		}
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
			const data = await apiFetch<{
				device_id: string;
				device_alias: string | null;
				lan_ip: string | null;
			}>('/system/info');

			deviceId = data.device_id;
			deviceAlias = data.device_alias ?? '';
			lanIp = data.lan_ip ?? '';
		} catch {
			deviceId = '';
			deviceAlias = '';
			lanIp = '';
		}
	}

	async function loadOperatingMode(): Promise<void> {
		try {
			const data = await apiFetch<{ mode: string }>('/system/mode');
			operatingMode = data.mode;
		} catch {
			operatingMode = '';
		}
	}

	onMount(() => {
		loadSystemInfo();
		loadHomeOnInit();
		loadOperatingMode();
	});
</script>

{#if $robotInit !== null && $robotInit.phase !== 'ready' && $robotInit.phase !== 'disabled'}
	<InitProgress init={$robotInit} />
{/if}

<div class="status-grid">
	<div class="status-column">
		<table>
			<thead>
				<tr>
					<th colspan="2">system information</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td class="label-cell">device id</td>
					<td class="value-cell">{deviceId || 'loading...'}</td>
				</tr>
				<tr>
					<td class="label-cell">device alias</td>
					<td class="value-cell">{deviceAlias || '—'}</td>
				</tr>
				<tr>
					<td class="label-cell">operating mode</td>
					<td class="value-cell">{operatingMode || '—'}</td>
				</tr>
				<tr>
					<td class="label-cell">lan ip</td>
					<td class="value-cell">{lanIp || 'loading...'}</td>
				</tr>
			</tbody>
		</table>

		<table>
			<thead>
				<tr>
					<th colspan="2">control loop override</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td class="label-cell">homing on startup</td>
					<td class="value-cell">
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
						<td class="label-cell">bypass safety guard</td>
						<td class="value-cell">
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
							<td class="label-cell">{component.label}</td>
							<td class="value-cell">
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
		</table>
	</div>

	<div class="status-column">
		{#if $robotStatus}
			<table>
				<thead>
					<tr>
						<th colspan="2">robot state</th>
					</tr>
				</thead>
				<tbody>
					<tr>
						<td class="label-cell">worker running</td>
						<td class="value-cell">
							<div class="status-action-cell">
								<span>{$robotStatus.running ? 'yes' : 'no'}</span>
								<button
									disabled={isRestartingApi === true}
									onclick={didClickRestartApi}>RESTART</button
								>
							</div>
							{#if restartApiError !== ''}
								<ErrorMessage message={restartApiError} />
							{/if}
						</td>
					</tr>
					<tr>
						<td class="label-cell">io box</td>
						<td class="value-cell">{getIoBoxOccupied() ? 'occupied' : 'empty'}</td>
					</tr>
					<tr>
						<td class="label-cell">light curtain</td>
						<td class="value-cell">
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
						<td class="label-cell">cabinet</td>
						<td class="value-cell">
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
						<td class="label-cell">home required</td>
						<td class="value-cell">
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
					<tr>
						<td class="label-cell">ack required</td>
						<td class="value-cell">
							<div class="status-action-cell">
								<span>{getSafetyAwaitingAck() ? 'yes' : 'no'}</span>
								<button
									disabled={getSafetyCanAcknowledge() !== true || isAcknowledgingSafety === true}
									onclick={didClickAcknowledgeSafety}>ACKNOWLEDGE</button
								>
							</div>
							{#if safetyAckError !== ''}
								<ErrorMessage message={safetyAckError} />
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
								<td class="label-cell">{entry.field}</td>
								<td class="value-cell">{entry.value}</td>
							</tr>
						{/each}
					</tbody>
				{/if}
			</table>
		{/if}
	</div>
</div>

<style>
	.status-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1.5em;
		align-items: start;
	}

	.status-column {
		display: flex;
		flex-direction: column;
		gap: 1.5em;
	}

	table {
		width: 100%;
	}

	.value-cell {
		width: 100%;
	}

	.label-cell {
		white-space: nowrap;
	}

	@media (max-width: 900px) {
		.status-grid {
			grid-template-columns: 1fr;
		}
	}

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
