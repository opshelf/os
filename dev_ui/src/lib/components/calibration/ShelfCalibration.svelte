<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getShelfStageExtension,
		saveShelfStageExtension,
		moveStage,
		homeMotor,
		type ShelfStageExtension
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let stageExtension: ShelfStageExtension | null = null;
	let frontValue = 0;
	let backValue = 0;
	let loading = false;
	let isSaving = false;
	let isMovingStage = false;
	let error = '';
	let message = '';

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadStageExtension();
	});

	async function loadStageExtension(): Promise<void> {
		loading = true;
		error = '';

		try {
			stageExtension = await getShelfStageExtension();
			frontValue = stageExtension.front;
			backValue = stageExtension.back;
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load shelf stage extension';
		} finally {
			loading = false;
		}
	}

	async function didClickSave(): Promise<void> {
		isSaving = true;
		error = '';
		message = '';

		try {
			await saveShelfStageExtension(frontValue, backValue);
			stageExtension = { front: frontValue, back: backValue };
			message = 'shelf stage extension saved';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to save shelf stage extension';
		} finally {
			isSaving = false;
		}
	}

	async function didClickReset(): Promise<void> {
		error = '';
		message = '';

		if (stageExtension !== null) {
			frontValue = stageExtension.front;
			backValue = stageExtension.back;
			message = 'values reset';
		}
	}

	async function didClickExtend(target: number): Promise<void> {
		isMovingStage = true;
		error = '';
		message = '';

		try {
			await moveStage(target);
			message = 'stage extended';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to extend stage';
		} finally {
			isMovingStage = false;
		}
	}

	async function didClickRetract(): Promise<void> {
		isMovingStage = true;
		error = '';
		message = '';

		try {
			await homeMotor('stage');
			message = 'stage retracted';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to retract stage';
		} finally {
			isMovingStage = false;
		}
	}
</script>

<table>
	<thead>
		<tr>
			<th colspan="2">shelf stage extension</th>
		</tr>
	</thead>
	<tbody>
		{#if loading === true}
			<tr>
				<td colspan="2">loading...</td>
			</tr>
		{:else if stageExtension !== null}
			<tr>
				<td>
					<span class="field-label">front</span>
					<input type="number" step="0.5" bind:value={frontValue} disabled={isSaving === true} />
					<button type="button" on:click={() => didClickExtend(frontValue)} disabled={isMovingStage === true}>extend</button>
				</td>
				<td>
					<span class="field-label">back</span>
					<input type="number" step="0.5" bind:value={backValue} disabled={isSaving === true} />
					<button type="button" on:click={() => didClickExtend(backValue)} disabled={isMovingStage === true}>extend</button>
				</td>
			</tr>
			<tr>
				<td colspan="2">
					<div class="action-buttons">
						<button type="button" on:click={didClickRetract} disabled={isMovingStage === true}>retract</button>
						<div class="divider"></div>
						<button type="button" on:click={didClickReset} disabled={isSaving === true}>
							reset
						</button>
						<button type="button" on:click={didClickSave} disabled={isSaving === true}>
							save
						</button>
					</div>
				</td>
			</tr>
		{/if}
	</tbody>
</table>

{#if message !== ''}
	<div class="message">{message}</div>
{/if}

{#if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.field-label {
		margin-right: 0.5em;
	}

	input[type='number'] {
		width: 5em;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.action-buttons button {
		margin-top: 0;
	}

	td button {
		margin-top: 0;
	}

	.message {
		margin-top: 1em;
	}

	.divider {
		width: 55px;
		height: 1px;
		border-top: 2px black dashed;
	}
</style>
