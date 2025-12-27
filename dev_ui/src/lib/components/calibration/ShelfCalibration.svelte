<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		getShelfStageExtension,
		saveShelfStageExtension,
		jogAxis,
		type ShelfStageExtension
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	let stageExtension: ShelfStageExtension | null = null;
	let frontValue = 0;
	let backValue = 0;
	let loading = false;
	let isSaving = false;
	let isExtending = false;
	let isStageExtended = false;
	let selectedSide: 'front' | 'back' = 'front';
	let currentSlidePosition = 0;
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

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'front' | 'back';
		isStageExtended = false;
	}

	async function didClickStageToggle(): Promise<void> {
		isExtending = true;
		error = '';
		message = '';

		try {
			const targetExtension = selectedSide === 'front' ? frontValue : backValue;

			if (isStageExtended === true) {
				const delta = 3 - currentSlidePosition;
				const result = await jogAxis('stage', delta);

				currentSlidePosition = result.slide_extension;
				isStageExtended = false;
				message = 'stage retracted';
			} else {
				const delta = targetExtension - currentSlidePosition;
				const result = await jogAxis('stage', delta);

				currentSlidePosition = result.slide_extension;
				isStageExtended = true;
				message = 'stage extended';
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to move stage';
		} finally {
			isExtending = false;
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
				</td>
				<td>
					<span class="field-label">back</span>
					<input type="number" step="0.5" bind:value={backValue} disabled={isSaving === true} />
				</td>
			</tr>
			<tr>
				<td colspan="2">
					<div class="action-buttons">
						<select value={selectedSide} on:change={didChangeSide} disabled={isExtending === true}>
							<option value="front">front</option>
							<option value="back">back</option>
						</select>
						<button
							type="button"
							on:click={didClickStageToggle}
							disabled={isSaving === true || isExtending === true}
						>
							{isStageExtended === true ? 'retract' : 'extend'}
						</button>
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

	.message {
		margin-top: 1em;
	}

	.divider {
		width: 30px;
		height: 1px;
		border-top: 2px black dashed;
	}
</style>
