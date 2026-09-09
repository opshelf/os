<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import ErrorMessage from './ErrorMessage.svelte';

	type Item = { id: number; name: string; upca: string; compartment: number | null };
	type Workflow = {
		id: string;
		state: string;
		operation: string;
		updated_at: string;
		error: string | null;
		item: Item;
		compartment: number | null;
		bin: { home_location: { side: string; shelf_idx: number; cell_idx: number } };
		contents?: Item[] | null;
	};
	let workflow: Workflow | null = null;
	let loading = false;
	let busy = false;
	let fresh = false;
	let expanded = false;
	let error = '';
	let resolution = '';
	let targetPresent = '';
	let confirmed = false;
	let confirmedIds: number[] = [];
	let disposed = false;
	$: home = workflow?.bin.home_location;
	$: homeLabel = home
		? `${home.side === 'left_module' ? 'L' : 'R'}:${home.shelf_idx}:${home.cell_idx}`
		: '';
	$: recoverable = workflow?.state === 'recovery_required';
	$: canReset =
		fresh &&
		recoverable &&
		!busy &&
		!loading &&
		confirmed &&
		(resolution === 'quarantined' ||
			(resolution === 'returned' &&
				(workflow?.operation === 'restore' ? workflow.contents != null : targetPresent !== '')));

	function clearConfirmation() {
		resolution = '';
		targetPresent = '';
		confirmed = false;
		confirmedIds = [];
	}

	async function refresh() {
		if (loading || busy || disposed) return;
		loading = true;
		try {
			const result = await apiFetch<{ workflow: Workflow | null }>('/storage/bin-workflows/active');
			if (disposed) return;
			if (
				result.workflow?.id !== workflow?.id ||
				result.workflow?.updated_at !== workflow?.updated_at
			) {
				clearConfirmation();
				expanded = false;
			}
			workflow = result.workflow;
			fresh = true;
		} catch (err) {
			fresh = false;
			expanded = false;
			clearConfirmation();
			error = err instanceof Error ? err.message : 'Failed to load tray workflow';
		} finally {
			loading = false;
		}
	}

	async function reset() {
		if (!canReset || !workflow) return;
		busy = true;
		error = '';
		try {
			await apiFetch(`/storage/bin-workflows/${encodeURIComponent(workflow.id)}/reconcile`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					resolution,
					...(resolution === 'returned'
						? workflow.operation === 'restore'
							? { confirmed_item_ids: confirmedIds }
							: { target_item_in_bin: targetPresent === 'yes' }
						: {})
				})
			});
			workflow = null;
			expanded = false;
			clearConfirmation();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to reset tray workflow';
		} finally {
			busy = false;
			await refresh();
		}
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => void refresh(), 5000);
		return () => {
			disposed = true;
			clearInterval(timer);
		};
	});
</script>

{#if fresh && recoverable && workflow}
	<tr>
		<td>tray reset required</td>
		<td>
			<div class="status-action-cell">
				<span>yes · {homeLabel}</span>
				<button
					disabled={!fresh || !recoverable || busy}
					onclick={() => {
						expanded = !expanded;
						clearConfirmation();
					}}
				>
					{expanded ? 'CANCEL RESET' : 'RESET'}
				</button>
			</div>
			{#if workflow.error}<ErrorMessage message={workflow.error} />{/if}
			{#if expanded}
				<p>
					This records manual recovery without moving the robot. Robot faults must be reset
					separately.
				</p>
				<fieldset disabled={busy}>
					<legend>Where is the tray now?</legend>
					<label
						><input
							type="radio"
							bind:group={resolution}
							value="returned"
							onchange={() => {
								confirmed = false;
							}}
						/>
						Returned to {homeLabel}</label
					>
					<label
						><input
							type="radio"
							bind:group={resolution}
							value="quarantined"
							onchange={() => {
								confirmed = false;
							}}
						/> Removed, unavailable, or no tray was used (test)</label
					>
					{#if resolution === 'returned'}
						{#if workflow.operation === 'restore'}
							<p>Select every item physically in the returned tray:</p>
							{#each workflow.contents ?? [] as item (item.id)}
								<label
									><input
										type="checkbox"
										bind:group={confirmedIds}
										value={item.id}
										onchange={() => {
											confirmed = false;
										}}
									/>
									{item.name} ({item.upca}) · compartment {item.compartment ?? '?'}</label
								>
							{/each}
						{:else}
							<label
								>Is {workflow.item.name} ({workflow.item.upca}) in compartment {workflow.compartment ??
									'—'}?
								<select
									bind:value={targetPresent}
									onchange={() => {
										confirmed = false;
									}}
								>
									<option value="">Select item state</option><option value="yes"
										>Yes, in the tray</option
									><option value="no">No, outside the tray</option>
								</select>
							</label>
						{/if}
					{:else if resolution === 'quarantined'}
						<p>The tray will be excluded from allocation and {homeLabel} marked for maintenance.</p>
					{/if}
					{#if resolution}
						<label
							><input type="checkbox" bind:checked={confirmed} /> I confirmed the tray location and contents;
							the tool and IO box are clear.</label
						>
					{/if}
					<button disabled={!canReset} onclick={reset}
						>{busy ? 'RESETTING…' : 'CONFIRM RESET'}</button
					>
				</fieldset>
			{/if}

			{#if error}<ErrorMessage message={error} />{/if}
		</td>
	</tr>
{/if}

<style>
	.status-action-cell {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1em;
	}
	label {
		display: block;
		margin: 0.75rem 0;
	}
	fieldset {
		margin-top: 1rem;
	}
	select {
		margin-left: 0.5rem;
	}
</style>
