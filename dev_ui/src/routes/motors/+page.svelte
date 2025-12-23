<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getMotors,
		simulateMotorCrash,
		controlGripper,
		controlDoor,
		type MotorList,
		type GripperState,
		type DoorAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let motors: MotorList | null = null;
	let isLoading = true;
	let crashingMotor: string | null = null;
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

	async function didClickSimulateCrash(motorName: string): Promise<void> {
		crashingMotor = motorName;
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
			error = err instanceof Error ? err.message : `failed to simulate crash for ${motorName}`;
		} finally {
			crashingMotor = null;
		}
	}

	async function didClickGripper(state: GripperState): Promise<void> {
		gripperAction = state;

		try {
			await controlGripper(state);
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
							on:click={() => didClickSimulateCrash(motor)}
							disabled={crashingMotor !== null}
						>
							{#if crashingMotor === motor}
								simulating...
							{:else}
								simulate crash
							{/if}
						</button>
					</td>
				</tr>
			{/each}
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
							on:click={() => didClickSimulateCrash(motor)}
							disabled={crashingMotor !== null}
						>
							{#if crashingMotor === motor}
								simulating...
							{:else}
								simulate crash
							{/if}
						</button>
					</td>
				</tr>
			{/each}
		</tbody>
		<thead>
			<tr>
				<th colspan="2">gripper</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>control</td>
				<td>
					<button
						type="button"
						on:click={() => didClickGripper('grip')}
						disabled={gripperAction !== null}
					>
						grip
					</button>
					<button
						type="button"
						on:click={() => didClickGripper('release')}
						disabled={gripperAction !== null}
					>
						release
					</button>
				</td>
			</tr>
		</tbody>
		<thead>
			<tr>
				<th colspan="2">door</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>control</td>
				<td>
					<button
						type="button"
						on:click={() => didClickDoor('open')}
						disabled={doorAction !== null}
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
						disabled={doorAction !== null}
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
						disabled={doorAction !== null}
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
