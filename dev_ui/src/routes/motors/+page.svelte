<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getMotors,
		simulateMotorCrash,
		homeMotor,
		controlGripper,
		controlDoor,
		probeMotors,
		moveMotor,
		type MotorList,
		type MotorProbeResult,
		type GripperState,
		type DoorAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let motors: MotorList | null = null;
	let isLoading = true;
	let activeMotor: string | null = null;
	let activeAction: string | null = null;
	let gripperAction: GripperState | null = null;
	let doorAction: DoorAction | null = null;
	let error = '';
	let message = '';

	let probeResults: Record<string, MotorProbeResult> = {};
	let isProbing = false;
	let moveTargets: Record<string, string> = {};
	let movingMotor: string | null = null;

	async function fetchMotors(): Promise<void> {
		try {
			motors = await getMotors();
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load motors';
		}
	}

	async function didClickCrash(motorName: string): Promise<void> {
		activeMotor = motorName;
		activeAction = 'crash';
		error = '';
		message = '';

		try {
			const result = await simulateMotorCrash(motorName);

			if (result.success === true) {
				message = `crash simulated for ${motorName}`;
			} else {
				error = `failed to simulate crash for ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `crash failed for ${motorName}`;
		} finally {
			activeMotor = null;
			activeAction = null;
		}
	}

	async function didClickHome(motorName: string): Promise<void> {
		activeMotor = motorName;
		activeAction = 'home';
		error = '';
		message = '';

		try {
			const result = await homeMotor(motorName);

			if (result.success === true) {
				message = `${motorName} homed`;
			} else {
				error = `failed to home ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `home failed for ${motorName}`;
		} finally {
			activeMotor = null;
			activeAction = null;
		}
	}

	async function didClickGripper(state: GripperState): Promise<void> {
		gripperAction = state;
		error = '';
		message = '';

		try {
			await controlGripper(state);
			message = `gripper ${state}`;
		} catch (err) {
			error = err instanceof Error ? err.message : `gripper ${state} failed`;
		} finally {
			gripperAction = null;
		}
	}

	async function didClickDoor(action: DoorAction): Promise<void> {
		doorAction = action;
		error = '';
		message = '';

		try {
			await controlDoor(action);
			message = `door ${action} complete`;
		} catch (err) {
			error = err instanceof Error ? err.message : `door ${action} failed`;
		} finally {
			doorAction = null;
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchMotors();
		} finally {
			isLoading = false;
		}
	}

	async function didClickProbe(): Promise<void> {
		isProbing = true;
		error = '';
		message = '';

		try {
			const response = await probeMotors();

			probeResults = {};
			for (const result of response.results) {
				probeResults[result.motor_name] = result;
			}

			message = 'probe complete';
		} catch (err) {
			error = err instanceof Error ? err.message : 'probe failed';
		} finally {
			isProbing = false;
		}
	}

	async function didClickMove(motorName: string): Promise<void> {
		const targetValue = moveTargets[motorName];

		if (targetValue === undefined || targetValue === '') {
			error = `enter a position for ${motorName}`;
			return;
		}

		const positionMm = parseFloat(targetValue);

		if (Number.isNaN(positionMm) === true) {
			error = `invalid position value for ${motorName}`;
			return;
		}

		movingMotor = motorName;
		error = '';
		message = '';

		try {
			const result = await moveMotor(motorName, positionMm);

			if (result.success === true) {
				message = `${motorName} moved to ${positionMm}mm`;
			} else {
				error = `failed to move ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `move failed for ${motorName}`;
		} finally {
			movingMotor = null;
		}
	}

	function didInputTarget(motorName: string, event: Event): void {
		const target = event.target as HTMLInputElement;
		moveTargets[motorName] = target.value;
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		initialLoad();
	});
</script>

{#if isLoading === true}
	<p>loading motors...</p>
{:else if motors !== null}
	<div class="actions">
		<button
			type="button"
			on:click={didClickProbe}
			disabled={isProbing === true || activeMotor !== null || movingMotor !== null}
		>
			{isProbing === true ? 'probing...' : 'probe all motors'}
		</button>
	</div>

	<table>
		<thead>
			<tr>
				<th colspan="4">io motors</th>
			</tr>
			<tr>
				<th>motor</th>
				<th>status</th>
				<th>move to</th>
				<th>actions</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.io.filter((m) => m !== 'door') as motor}
				<tr>
					<td>{motor}</td>
					<td>
						{#if probeResults[motor] !== undefined}
							{#if probeResults[motor].responsive === true}
								<span class="status-ok">{probeResults[motor].position_mm?.toFixed(2)}mm</span>
							{:else}
								<span class="status-error">no response</span>
							{/if}
						{:else}
							<span class="status-unknown">-</span>
						{/if}
					</td>
					<td>
						<input
							type="number"
							step="0.1"
							placeholder="mm"
							value={moveTargets[motor] ?? ''}
							on:input={(e) => didInputTarget(motor, e)}
							disabled={movingMotor !== null || activeMotor !== null}
						/>
						<button
							type="button"
							on:click={() => didClickMove(motor)}
							disabled={movingMotor !== null || activeMotor !== null}
						>
							{movingMotor === motor ? '...' : 'go'}
						</button>
					</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null || doorAction !== null || movingMotor !== null}
						>
							home
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null || doorAction !== null || movingMotor !== null}
						>
							crash
						</button>
					</td>
				</tr>
			{/each}
			<tr>
				<td>door</td>
				<td>
					{#if probeResults['door'] !== undefined}
						{#if probeResults['door'].responsive === true}
							<span class="status-ok">{probeResults['door'].position_mm?.toFixed(2)}mm</span>
						{:else}
							<span class="status-error">no response</span>
						{/if}
					{:else}
						<span class="status-unknown">-</span>
					{/if}
				</td>
				<td>
					<input
						type="number"
						step="0.1"
						placeholder="mm"
						value={moveTargets['door'] ?? ''}
						on:input={(e) => didInputTarget('door', e)}
						disabled={movingMotor !== null || activeMotor !== null}
					/>
					<button
						type="button"
						on:click={() => didClickMove('door')}
						disabled={movingMotor !== null || activeMotor !== null}
					>
						{movingMotor === 'door' ? '...' : 'go'}
					</button>
				</td>
				<td>
					<button
						type="button"
						on:click={() => didClickHome('door')}
						disabled={activeMotor !== null || doorAction !== null || movingMotor !== null}
					>
						home
					</button>
					<button
						type="button"
						on:click={() => didClickCrash('door')}
						disabled={activeMotor !== null || doorAction !== null || movingMotor !== null}
					>
						crash
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('open')}
						disabled={doorAction !== null || activeMotor !== null || movingMotor !== null}
					>
						open
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('close')}
						disabled={doorAction !== null || activeMotor !== null || movingMotor !== null}
					>
						close
					</button>
				</td>
			</tr>
		</tbody>

		<thead>
			<tr>
				<th colspan="4">tool motors</th>
			</tr>
			<tr>
				<th>motor</th>
				<th>status</th>
				<th>move to</th>
				<th>actions</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.tool as motor}
				<tr>
					<td>{motor}</td>
					<td>
						{#if probeResults[motor] !== undefined}
							{#if probeResults[motor].responsive === true}
								<span class="status-ok">{probeResults[motor].position_mm?.toFixed(2)}mm</span>
							{:else}
								<span class="status-error">no response</span>
							{/if}
						{:else}
							<span class="status-unknown">-</span>
						{/if}
					</td>
					<td>
						<input
							type="number"
							step="0.1"
							placeholder="mm"
							value={moveTargets[motor] ?? ''}
							on:input={(e) => didInputTarget(motor, e)}
							disabled={movingMotor !== null || activeMotor !== null}
						/>
						<button
							type="button"
							on:click={() => didClickMove(motor)}
							disabled={movingMotor !== null || activeMotor !== null}
						>
							{movingMotor === motor ? '...' : 'go'}
						</button>
					</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null || gripperAction !== null || movingMotor !== null}
						>
							home
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null || gripperAction !== null || movingMotor !== null}
						>
							crash
						</button>
					</td>
				</tr>
			{/each}
			<tr>
				<td>gripper</td>
				<td>-</td>
				<td>-</td>
				<td>
					<button
						type="button"
						on:click={() => didClickGripper('grip')}
						disabled={gripperAction !== null || activeMotor !== null || movingMotor !== null}
					>
						grip
					</button>
					<button
						type="button"
						on:click={() => didClickGripper('release')}
						disabled={gripperAction !== null || activeMotor !== null || movingMotor !== null}
					>
						release
					</button>
				</td>
			</tr>
		</tbody>
	</table>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{:else if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.actions {
		margin-bottom: 1rem;
	}

	button {
		margin-top: 0;
	}

	input[type='number'] {
		width: 80px;
		margin-right: 4px;
	}

	.status-ok {
		color: green;
	}

	.status-error {
		color: red;
	}

	.status-unknown {
		color: gray;
	}
</style>
