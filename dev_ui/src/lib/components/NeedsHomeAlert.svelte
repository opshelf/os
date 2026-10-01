<script lang="ts">
	import { homeMotor } from '$lib/api';
	import { devicePath } from '$lib/nav';
	import { robotStatus } from '$lib/stores';

	// The global /robot/status stream is the single source of truth: a motor
	// crash publishes a snapshot with needs_home + needs_home_axes immediately,
	// and a successful home clears the flag and republishes.
	const needsHome = $derived($robotStatus?.needs_home === true);
	const motors = $derived(needsHome ? [...($robotStatus?.needs_home_axes ?? [])].sort() : []);
	const reason = $derived($robotStatus?.needs_home_reason ?? '');
	const motorsKey = $derived(motors.length > 0 ? motors.join(':') : `reason:${reason}`);

	// Dismissal is tracked per motor set so a cancelled alert does not reappear
	// on the next snapshot, while a new crash (a different set) re-opens it.
	let dismissedKey = $state<string | null>(null);
	let isHoming = $state<boolean>(false);
	let homeError = $state<string>('');

	const isVisible = $derived(needsHome && dismissedKey !== motorsKey);

	$effect(() => {
		if (needsHome === false) {
			dismissedKey = null;
			isHoming = false;
			homeError = '';
		}
	});

	async function didHome(): Promise<void> {
		if (isHoming === true || motors.length === 0) {
			return;
		}
		isHoming = true;
		homeError = '';
		// One motor at a time: the server guards concurrent motion.
		for (const motor of motors) {
			try {
				await homeMotor(motor);
			} catch (err) {
				homeError = `${err instanceof Error ? err.message : 'home failed'} (during ${motor})`;
				break;
			}
		}
		isHoming = false;
	}

	function didCancel(): void {
		dismissedKey = motorsKey;
	}
</script>

{#if isVisible}
	<div class="needs-home-scrim"></div>
	<aside class="needs-home-alert" role="alert">
		<p class="needs-home-title">
			motor needs homing{motors.length > 0 ? `: ${motors.join(', ')}` : ''}
		</p>
		<div class="needs-home-actions">
			<button type="button" onclick={didCancel} disabled={isHoming === true}>
				cancel
			</button>
			<div class="needs-home-spacer"></div>
			{#if motors.length > 0}
				<button type="button" onclick={() => void didHome()} disabled={isHoming === true}>
					{isHoming ? 'homing...' : 'home'}
				</button>
			{:else}
				<a class="needs-home-link" href={devicePath('/motors')}>open motors page</a>
			{/if}
		</div>
		{#if homeError !== ''}
			<p class="needs-home-error">{homeError}</p>
		{/if}
	</aside>
{/if}

<style>
	.needs-home-scrim {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.4);
		pointer-events: none;
		z-index: 99;
	}

	.needs-home-alert {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		z-index: 100;
		min-width: 24em;
		max-width: min(36em, 90vw);
		padding: 1.5em;
		background: white;
		border: 2px solid black;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
		text-transform: uppercase;
	}

	.needs-home-title {
		margin: 0;
	}

	.needs-home-actions {
		display: flex;
		gap: 1em;
		align-items: center;
		margin-top: 1.5em;
	}

	.needs-home-spacer {
		flex: 1;
	}

	.needs-home-actions button,
	.needs-home-link {
		margin-top: 0;
	}

	.needs-home-error {
		margin-top: 1em;
		max-width: 32em;
		color: #c40000;
		text-transform: none;
		white-space: pre-wrap;
	}
</style>
