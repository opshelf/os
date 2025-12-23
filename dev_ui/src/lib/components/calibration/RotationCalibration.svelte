<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getRotationAngles,
		rotateToAngle,
		saveRotationAngles,
		type RotationAngles
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let angles: RotationAngles | null = null;
	let loading = false;
	let error = '';
	let message = '';
	let isRotating = false;
	let isSaving = false;

	let frontAngle = 0;
	let backAngle = 0;

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadAngles();
	});

	async function loadAngles(): Promise<void> {
		loading = true;
		error = '';

		try {
			angles = await getRotationAngles();
			frontAngle = angles.front;
			backAngle = angles.back;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load rotation angles';
		} finally {
			loading = false;
		}
	}

	async function didClickRotateFront(): Promise<void> {
		isRotating = true;
		error = '';
		message = '';

		try {
			await rotateToAngle(frontAngle);
			message = `rotated to front angle ${frontAngle} deg`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to rotate';
		} finally {
			isRotating = false;
		}
	}

	async function didClickRotateBack(): Promise<void> {
		isRotating = true;
		error = '';
		message = '';

		try {
			await rotateToAngle(backAngle);
			message = `rotated to back angle ${backAngle} deg`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to rotate';
		} finally {
			isRotating = false;
		}
	}

	async function didClickSave(): Promise<void> {
		isSaving = true;
		error = '';
		message = '';

		try {
			await saveRotationAngles(frontAngle, backAngle);
			message = 'rotation angles saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save rotation angles';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		error = '';
		message = '';

		try {
			angles = await getRotationAngles();
			frontAngle = angles.front;
			backAngle = angles.back;
			message = 'angles reset from config';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to reset angles';
		}
	}

	function didChangeFrontAngle(event: Event): void {
		const target = event.target as HTMLInputElement;

		frontAngle = parseFloat(target.value);
	}

	function didChangeBackAngle(event: Event): void {
		const target = event.target as HTMLInputElement;

		backAngle = parseFloat(target.value);
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="3">rotation calibration</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="3">loading rotation angles...</td>
			</tr>
		{:else if angles !== null}
			<tr>
				<td class="label-cell">rotation angle a (front)</td>
				<td>
					<input
						type="number"
						step="0.1"
						value={frontAngle}
						on:change={didChangeFrontAngle}
						disabled={isRotating === true || isSaving === true}
					/>
					<span class="unit">deg</span>
				</td>
				<td>
					<button
						type="button"
						on:click={didClickRotateFront}
						disabled={isRotating === true || isSaving === true}
					>
						rotate
					</button>
				</td>
			</tr>
			<tr>
				<td class="label-cell">rotation angle b (back)</td>
				<td>
					<input
						type="number"
						step="0.1"
						value={backAngle}
						on:change={didChangeBackAngle}
						disabled={isRotating === true || isSaving === true}
					/>
					<span class="unit">deg</span>
				</td>
				<td>
					<button
						type="button"
						on:click={didClickRotateBack}
						disabled={isRotating === true || isSaving === true}
					>
						rotate
					</button>
				</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if angles !== null}
	<div class="action-buttons">
		<button
			type="button"
			on:click={didClickReset}
			disabled={isRotating === true || isSaving === true}
		>
			reset
		</button>
		<button
			type="button"
			on:click={didClickSave}
			disabled={isRotating === true || isSaving === true}
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
	.label-cell {
		font-weight: bold;
		white-space: nowrap;
	}

	input[type='number'] {
		width: 5em;
	}

	.unit {
		margin-left: 0.25em;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		margin-top: 1.5em;
	}

	.message {
		margin-top: 1em;
	}
</style>
