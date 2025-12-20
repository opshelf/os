<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getIOBoxCalibration,
		moveToIOBoxPosition,
		jogIOBoxAxis,
		testIOBoxSequence,
		saveIOBoxCalibration,
		type IOBoxPosition
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let ioboxPosition: IOBoxPosition | null = null;
	let loading = false;
	let error = '';
	let message = '';
	let selectedPosition: 'low' | 'high' = 'low';
	let isMoving = false;
	let isTesting = false;
	let isSaving = false;
	let isPositioned = false;
	let jogStep = 1;

	const JOG_STEPS = [
		{ value: 0.1, label: '0.1mm' },
		{ value: 0.25, label: '0.25mm' },
		{ value: 0.5, label: '0.5mm' },
		{ value: 1, label: '1mm' },
		{ value: 2, label: '2mm' },
		{ value: 5, label: '5mm' },
		{ value: 10, label: '1cm' },
		{ value: 100, label: '10cm' }
	];

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadCalibration();
	});

	async function loadCalibration(): Promise<void> {
		loading = true;
		error = '';

		try {
			ioboxPosition = await getIOBoxCalibration();
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load io box calibration';
		} finally {
			loading = false;
		}
	}

	function didChangePosition(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedPosition = target.value as 'low' | 'high';
		isPositioned = false;
		error = '';
		message = '';
	}

	async function didClickMove(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';

		try {
			ioboxPosition = await moveToIOBoxPosition(selectedPosition);
			isPositioned = true;
			message = `moved to ${selectedPosition} position`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to move to io box position';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: 'x' | 'y' | 'z' | 'stage', delta: number): Promise<void> {
		isMoving = true;
		error = '';

		try {
			ioboxPosition = await jogIOBoxAxis(selectedPosition, axis, delta);
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to jog io box axis';
		} finally {
			isMoving = false;
		}
	}

	async function didClickTest(): Promise<void> {
		isTesting = true;
		error = '';
		message = '';

		try {
			await testIOBoxSequence();
			message = 'io box test sequence completed';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to run io box test sequence';
		} finally {
			isTesting = false;
		}
	}

	async function didClickSave(): Promise<void> {
		if (ioboxPosition === null) {
			error = 'no io box position to save';
			return;
		}

		isSaving = true;
		error = '';
		message = '';

		try {
			await saveIOBoxCalibration(ioboxPosition);
			message = 'io box calibration saved and robot homed';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save io box calibration';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';

		try {
			ioboxPosition = await getIOBoxCalibration();
			message = 'io box position reset from config';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to reset io box position';
		} finally {
			isMoving = false;
		}
	}

	function formatNumber(value: number): string {
		return value.toFixed(2);
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="4">io box calibration</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="4">loading io box calibration...</td>
			</tr>
		{:else if ioboxPosition !== null}
			<tr>
				<td colspan="2">
					<span class="field-label">position</span>
					<select value={selectedPosition} on:change={didChangePosition}>
						<option value="low">pickup low</option>
						<option value="high">pickup high</option>
					</select>
				</td>
				<td colspan="2">
					<button
						class="move-button"
						type="button"
						on:click={didClickMove}
						disabled={isMoving === true || isTesting === true}
					>
						move
					</button>
				</td>
			</tr>
			<tr>
				<td>low x: {formatNumber(ioboxPosition.pickup_low_x)}</td>
				<td>low y: {formatNumber(ioboxPosition.pickup_low_y)}</td>
				<td>high x: {formatNumber(ioboxPosition.pickup_high_x)}</td>
				<td>high y: {formatNumber(ioboxPosition.pickup_high_y)}</td>
			</tr>
			<tr>
				<td colspan="2">Z: {formatNumber(ioboxPosition.gripper_extension)}</td>
				<td colspan="2">stage offset: {formatNumber(ioboxPosition.io_stage_offset)}</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if ioboxPosition !== null}
	{#if isPositioned === true}
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">X</span>
				<button type="button" on:click={() => didClickJog('x', jogStep)} disabled={isMoving === true}>
					-
				</button>
				<button type="button" on:click={() => didClickJog('x', -jogStep)} disabled={isMoving === true}>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">Y</span>
				<button type="button" on:click={() => didClickJog('y', -jogStep)} disabled={isMoving === true}>
					-
				</button>
				<button type="button" on:click={() => didClickJog('y', jogStep)} disabled={isMoving === true}>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-axis">
				<span class="axis-label">Z</span>
				<button
					type="button"
					on:click={() => didClickJog('z', -jogStep)}
					disabled={isMoving === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('z', jogStep)}
					disabled={isMoving === true}
				>
					+
				</button>
			</div>
			<div class="jog-axis">
				<span class="axis-label">stage</span>
				<button
					type="button"
					on:click={() => didClickJog('stage', -jogStep)}
					disabled={isMoving === true}
				>
					-
				</button>
				<button
					type="button"
					on:click={() => didClickJog('stage', jogStep)}
					disabled={isMoving === true}
				>
					+
				</button>
			</div>
		</div>
		<div class="jog-controls">
			<div class="jog-step">
				<span class="axis-label">step</span>
				<select bind:value={jogStep}>
					{#each JOG_STEPS as step}
						<option value={step.value}>{step.label}</option>
					{/each}
				</select>
			</div>
		</div>
	{/if}

	<div class="action-buttons">
		<button
			type="button"
			on:click={didClickTest}
			disabled={isMoving === true || isTesting === true || isSaving === true}
		>
			test sequence
		</button>
		<button
			type="button"
			on:click={didClickReset}
			disabled={isMoving === true || isTesting === true || isSaving === true}
		>
			reset
		</button>
		<button
			type="button"
			on:click={didClickSave}
			disabled={isMoving === true || isTesting === true || isSaving === true}
		>
			save
		</button>
	</div>
{/if}

{#if message !== ''}
	<p class="message">{message}</p>
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.field-label {
		margin-right: 0.5em;
	}

	.jog-controls {
		display: flex;
		gap: 2em;
		margin-top: 2em;
		margin-bottom: 1em;
	}

	.jog-axis,
	.jog-step {
		display: flex;
		align-items: center;
		gap: 0.5em;
	}

	.axis-label {
		font-weight: bold;
		min-width: 1.5em;
	}

	.jog-axis button {
		min-width: 3em;
		min-height: 3em;
		font-size: 1.5em;
		margin-top: 0;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1em;
	}

	.message {
		margin-top: 1em;
		color: green;
	}

	.move-button {
		margin-top: 0;
	}
</style>
