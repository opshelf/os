<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getFridgeDoorCalibration,
		moveFridgeDoor,
		saveFridgeDoorCalibration,
		type FridgeDoorCalibration
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let calibration: FridgeDoorCalibration | null = null;
	let loading = false;
	let error = '';
	let message = '';
	let movingPosition: 'left' | 'center' | 'right' | null = null;
	let isSaving = false;

	let leftPosition = 0;
	let centerPosition = 0;
	let rightPosition = 0;

	onMount(function () {
		if (browser === true) {
			void loadCalibration();
		}
	});

	async function loadCalibration(): Promise<void> {
		loading = true;
		error = '';

		try {
			calibration = await getFridgeDoorCalibration();
			leftPosition = calibration.left;
			centerPosition = calibration.center;
			rightPosition = calibration.right;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load fridge calibration';
		} finally {
			loading = false;
		}
	}

	async function move(
		positionName: 'left' | 'center' | 'right',
		position: number
	): Promise<void> {
		movingPosition = positionName;
		error = '';
		message = '';

		try {
			await moveFridgeDoor(position);
			message = `fridge door moved to ${positionName} position (${position} mm)`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to move fridge door';
		} finally {
			movingPosition = null;
		}
	}

	async function didClickSave(): Promise<void> {
		isSaving = true;
		error = '';
		message = '';

		try {
			await saveFridgeDoorCalibration(leftPosition, centerPosition, rightPosition);
			calibration = await getFridgeDoorCalibration();
			message = 'fridge door positions saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save fridge door positions';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		message = '';
		await loadCalibration();
		if (error === '') {
			message = 'fridge door positions reset from config';
		}
	}

	function isBusy(): boolean {
		return movingPosition !== null || isSaving === true;
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="2">FRIDGE DOOR</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="2">loading fridge calibration...</td>
			</tr>
		{:else if calibration !== null}
			<tr>
				<td class="label-cell">left position</td>
				<td>
					<input
						type="number"
						step="0.1"
						min={calibration.lower_limit}
						max={calibration.upper_limit}
						bind:value={leftPosition}
						disabled={isBusy()}
					/>
					<button type="button" on:click={() => move('left', leftPosition)} disabled={isBusy()}>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td class="label-cell">center position</td>
				<td>
					<input
						type="number"
						step="0.1"
						min={calibration.lower_limit}
						max={calibration.upper_limit}
						bind:value={centerPosition}
						disabled={isBusy()}
					/>
					<button type="button" on:click={() => move('center', centerPosition)} disabled={isBusy()}>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td class="label-cell">right position</td>
				<td>
					<input
						type="number"
						step="0.1"
						min={calibration.lower_limit}
						max={calibration.upper_limit}
						bind:value={rightPosition}
						disabled={isBusy()}
					/>
					<button type="button" on:click={() => move('right', rightPosition)} disabled={isBusy()}>
						move
					</button>
				</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if calibration !== null}
	<div class="action-buttons">
		<button type="button" on:click={didClickSave} disabled={isBusy()}>save</button>
		<button type="button" on:click={didClickReset} disabled={isBusy()}>reset</button>
	</div>
{/if}

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.label-cell {
		white-space: nowrap;
	}

	input[type='number'] {
		width: 7em;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1.5em;
	}

	button {
		margin: 0;
	}
</style>
