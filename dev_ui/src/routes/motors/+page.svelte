<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { getMotors, simulateMotorCrash, type MotorList } from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let motors: MotorList | null = null;
	let isLoading = true;
	let crashingMotor: string | null = null;
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
		color: green;
	}

	button {
		margin-top: 0;
	}
</style>
