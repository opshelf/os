<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getMotors,
		simulateMotorCrash,
		homeMotor,
		controlGripper,
		controlDoor,
		type MotorList,
		type GripperState,
		type DoorAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let motors: MotorList | null = null;
	let isLoading = true;
	let activeMotor: string | null = null;
	let activeAction: string | null = null;
	let gripperAction: GripperState | null = null;
	let doorAction: DoorAction | null = null;
	let error = '';
	let message = '';

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
	<table>
		<thead>
			<tr>
				<th colspan="2">io motors</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.io as motor}
				<tr>
					<td>{motor}</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null || doorAction !== null}
						>
							{#if activeMotor === motor && activeAction === 'home'}
								homing...
							{:else}
								home
							{/if}
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null || doorAction !== null}
						>
							{#if activeMotor === motor && activeAction === 'crash'}
								crashing...
							{:else}
								crash
							{/if}
						</button>
					</td>
				</tr>
			{/each}
			<tr>
				<td>door</td>
				<td>
					<button
						type="button"
						on:click={() => didClickDoor('open')}
						disabled={doorAction !== null || activeMotor !== null}
					>
						{#if doorAction === 'open'}
							opening...
						{:else}
							open
						{/if}
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('close')}
						disabled={doorAction !== null || activeMotor !== null}
					>
						{#if doorAction === 'close'}
							closing...
						{:else}
							close
						{/if}
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('home')}
						disabled={doorAction !== null || activeMotor !== null}
					>
						{#if doorAction === 'home'}
							homing...
						{:else}
							home
						{/if}
					</button>
				</td>
			</tr>
		</tbody>

		<thead>
			<tr>
				<th colspan="2">tool motors</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.tool as motor}
				<tr>
					<td>{motor}</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null || gripperAction !== null}
						>
							{#if activeMotor === motor && activeAction === 'home'}
								homing...
							{:else}
								home
							{/if}
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null || gripperAction !== null}
						>
							{#if activeMotor === motor && activeAction === 'crash'}
								crashing...
							{:else}
								crash
							{/if}
						</button>
					</td>
				</tr>
			{/each}
			<tr>
				<td>gripper</td>
				<td>
					<button
						type="button"
						on:click={() => didClickGripper('grip')}
						disabled={gripperAction !== null || activeMotor !== null}
					>
						{#if gripperAction === 'grip'}
							gripping...
						{:else}
							grip
						{/if}
					</button>
					<button
						type="button"
						on:click={() => didClickGripper('release')}
						disabled={gripperAction !== null || activeMotor !== null}
					>
						{#if gripperAction === 'release'}
							releasing...
						{:else}
							release
						{/if}
					</button>
				</td>
			</tr>
		</tbody>
	</table>

	{#if message !== ''}
		<p class="message">{message}</p>
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{:else if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.message {
		margin-top: 1em;
	}

	button {
		margin-top: 0;
	}
</style>
