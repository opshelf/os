<script lang="ts">
	import { acknowledgeSafety, ApiError } from '$lib/api';
	import { robotStatus } from '$lib/stores';

	// The global /robot/status stream is the single source of truth. The
	// server decides can_acknowledge: an interruption that is over on every
	// source the operator can physically clear, including sources that only
	// an acknowledgement can clear (e.g. the scale weight-change guard), so
	// the same component works with or without the motion guard.
	const safety = $derived(
		$robotStatus?.robot_status?.safety ?? null
	);
	const canAcknowledge = $derived(
		safety !== null && (safety as Record<string, unknown>).can_acknowledge === true
	);
	const interruptionId = $derived(
		safety !== null ? ((safety as Record<string, unknown>).interruption_id as number) ?? 0 : 0
	);
	const activeSources = $derived(
		canAcknowledge ? [...(((safety as Record<string, unknown>).active_sources as string[]) ?? []).sort()] : []
	);

	// Dismissal is tracked per interruption episode so a cancelled alert does
	// not reappear on the next snapshot, while a new interruption (a different
	// episode id) re-opens it.
	let dismissedId = $state<number | null>(null);
	let isAcknowledging = $state<boolean>(false);
	let ackError = $state<string>('');

	const isVisible = $derived(canAcknowledge && dismissedId !== interruptionId);

	$effect(() => {
		if (canAcknowledge === false) {
			dismissedId = null;
			isAcknowledging = false;
			ackError = '';
		}
	});

	async function didAcknowledge(): Promise<void> {
		if (isAcknowledging === true) {
			return;
		}
		isAcknowledging = true;
		ackError = '';

		try {
			await acknowledgeSafety();
		} catch (err) {
			if (err instanceof ApiError && err.status === 409 && err.detail?.active_sources?.length) {
				ackError = `cannot acknowledge: ${err.detail.active_sources.join(', ')}`;
			} else {
				ackError = err instanceof Error ? err.message : 'acknowledge failed';
			}
		} finally {
			isAcknowledging = false;
		}
	}

	function didCancel(): void {
		dismissedId = interruptionId;
	}
</script>

{#if isVisible}
	<div class="safety-ack-scrim"></div>
	<aside class="safety-ack-alert" role="alert">
		<p class="safety-ack-title">
			safety interruption{activeSources.length > 0 ? `: ${activeSources.join(', ')}` : ''}
		</p>
		<p class="safety-ack-subtitle">acknowledgement required</p>
		<div class="safety-ack-actions">
			<button type="button" onclick={didCancel} disabled={isAcknowledging === true}>
				cancel
			</button>
			<div class="safety-ack-spacer"></div>
			<button type="button" onclick={() => void didAcknowledge()} disabled={isAcknowledging === true}>
				{isAcknowledging ? 'acknowledging...' : 'acknowledge'}
			</button>
		</div>
		{#if ackError !== ''}
			<p class="safety-ack-error">{ackError}</p>
		{/if}
	</aside>
{/if}

<style>
	.safety-ack-scrim {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.4);
		pointer-events: none;
		z-index: 99;
	}

	.safety-ack-alert {
		position: fixed;
		top: calc(50% - 13em);
		left: 50%;
		transform: translate(-50%, -50%);
		z-index: 101;
		min-width: 24em;
		max-width: min(36em, 90vw);
		padding: 1.5em;
		background: white;
		border: 2px solid black;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
		text-transform: uppercase;
	}

	.safety-ack-title {
		margin: 0;
	}

	.safety-ack-subtitle {
		margin: 0.5em 0 0;
	}

	.safety-ack-actions {
		display: flex;
		gap: 1em;
		align-items: center;
		margin-top: 1.5em;
	}

	.safety-ack-spacer {
		flex: 1;
	}

	.safety-ack-actions button {
		margin-top: 0;
	}

	.safety-ack-error {
		margin-top: 1em;
		max-width: 32em;
		color: #c40000;
		text-transform: none;
		white-space: pre-wrap;
	}
</style>
