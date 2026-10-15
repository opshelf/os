<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		getCalibrationStatus,
		getShelfStageExtension,
		saveShelfStageExtension,
		moveStage,
		homeMotor,
		type CalibrationStatus,
		type ShelfStageExtension
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	export let disabled = false;

	const dispatch = createEventDispatcher<{ needsHome: CalibrationStatus }>();

	let stageExtension: ShelfStageExtension | null = null;
	let frontValue = 0;
	let backValue = 0;
	let loading = false;
	let isSaving = false;
	let isMovingStage = false;
	let error = '';
	let message = '';
	let warning = '';

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadStageExtension();
	});

	async function loadStageExtension(): Promise<void> {
		loading = true;
		error = '';
		warning = '';

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
		warning = '';

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
		warning = '';

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
		warning = '';

		try {
			const result = await moveStage(target);
			message = 'stage extended';
			warning = result.warnings && result.warnings.length > 0 ? result.warnings.join('; ') : '';
		} catch (err) {
			await notifyNeedsHome(err);
			error = err instanceof Error ? err.message : 'failed to extend stage';
		} finally {
			isMovingStage = false;
		}
	}

	async function didClickRetract(): Promise<void> {
		isMovingStage = true;
		error = '';
		message = '';
		warning = '';

		try {
			await homeMotor('stage');
			message = 'stage retracted';
		} catch (err) {
			await notifyNeedsHome(err);
			error = err instanceof Error ? err.message : 'failed to retract stage';
		} finally {
			isMovingStage = false;
		}
	}

	async function notifyNeedsHome(err: unknown): Promise<void> {
		if (err instanceof ApiError && err.detail?.needs_home) {
			dispatch('needsHome', {
				needs_home: true,
				needs_home_reason: err.detail.reason ?? err.detail.message ?? '',
				needs_home_axes: err.detail.needs_home_axes ?? []
			});
			return;
		}

		try {
			const status = await getCalibrationStatus();
			dispatch('needsHome', status);
		} catch {
			// Ignore status polling failures; caller will surface the error.
		}
	}
</script>

<div class="scroll-x">
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
						<input
							type="number"
							step="0.5"
							bind:value={frontValue}
							disabled={isSaving === true || disabled === true}
						/>
						<button
							type="button"
							on:click={() => didClickExtend(frontValue)}
							disabled={isMovingStage === true || disabled === true}>extend</button
						>
					</td>
					<td>
						<span class="field-label">back</span>
						<input
							type="number"
							step="0.5"
							bind:value={backValue}
							disabled={isSaving === true || disabled === true}
						/>
						<button
							type="button"
							on:click={() => didClickExtend(backValue)}
							disabled={isMovingStage === true || disabled === true}>extend</button
						>
					</td>
				</tr>
				<tr>
					<td colspan="2">
						<div class="action-buttons">
							<button
								type="button"
								on:click={didClickRetract}
								disabled={isMovingStage === true || disabled === true}>retract</button
							>
							<button
								type="button"
								on:click={didClickReset}
								disabled={isSaving === true || disabled === true}
							>
								reset
							</button>
							<button
								type="button"
								on:click={didClickSave}
								disabled={isSaving === true || disabled === true}
							>
								save
							</button>
						</div>
					</td>
				</tr>
			{/if}
		</tbody>
	</table>
</div>

{#if message !== ''}
	<StatusMessage {message} />
{/if}

{#if warning !== ''}
	<StatusMessage message={`warning: ${warning}`} />
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
</style>
