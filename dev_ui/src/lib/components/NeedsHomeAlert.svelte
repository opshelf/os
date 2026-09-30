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
	let homingMotor = $state<string | null>(null);
	let homeError = $state<string>('');

	const isVisible = $derived(needsHome && dismissedKey !== motorsKey);

	$effect(() => {
		if (needsHome === false) {
			dismissedKey = null;
			homeError = '';
		}
	});

	async function didHomeMotor(motor: string): Promise<void> {
		if (homingMotor !== null) {
			return;
		}
		homingMotor = motor;
		homeError = '';
		try {
			await homeMotor(motor);
		} catch (err) {
			homeError = err instanceof Error ? err.message : `failed to home ${motor}`;
		} finally {
			homingMotor = null;
		}
	}

	function didCancel(): void {
		dismissedKey = motorsKey;
	}
</script>

{#if isVisible}
	<aside class="needs-home-alert" role="alert">
		<p class="needs-home-title">
			motor needs homing{motors.length > 0 ? `: ${motors.join(', ')}` : ''}
		</p>
		{#if reason !== ''}
			<p class="needs-home-reason">{reason}</p>
		{/if}
		<div class="needs-home-actions">
			{#each motors as motor (motor)}
				<button
					type="button"
					onclick={() => void didHomeMotor(motor)}
					disabled={homingMotor !== null}
				>
					{homingMotor === motor ? `homing ${motor}...` : `home ${motor}`}
				</button>
			{/each}
			{#if motors.length === 0}
				<a class="needs-home-link" href={devicePath('/motors')}>open motors page</a>
			{/if}
			<button type="button" onclick={didCancel} disabled={homingMotor !== null}>
				cancel
			</button>
		</div>
		{#if homeError !== ''}
			<p class="needs-home-error">{homeError}</p>
		{/if}
	</aside>
{/if}

<style>
	.needs-home-alert {
		position: fixed;
		top: 5em;
		left: 50%;
		transform: translateX(-50%);
		z-index: 100;
		max-width: 36em;
		max-width: min(36em, 90vw);
		padding: 1em 1.5em;
		background: white;
		border: 2px solid black;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
		text-transform: uppercase;
	}

	.needs-home-title {
		margin-bottom: 0.5em;
		font-weight: bold;
	}

	.needs-home-reason {
		margin-bottom: 0.5em;
		max-width: 32em;
		text-transform: none;
		white-space: pre-wrap;
	}

	.needs-home-actions {
		display: flex;
		gap: 0.5em;
		align-items: center;
		flex-wrap: wrap;
	}

	.needs-home-actions button,
	.needs-home-link {
		margin-top: 0;
	}

	.needs-home-error {
		margin-top: 0.5em;
		max-width: 32em;
		color: #c40000;
		text-transform: none;
		white-space: pre-wrap;
	}
</style>
