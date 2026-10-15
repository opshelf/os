<script lang="ts">
	import { homeMotor } from '$lib/api';
	import { devicePath } from '$lib/nav';
	import { robotStatus } from '$lib/stores';
	import { postToast } from '$lib/toasts';

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
	let queuedMotors = $state<string[]>([]);

	const isVisible = $derived(needsHome && dismissedKey !== motorsKey);
	const isBusy = $derived(homingMotor !== null || queuedMotors.length > 0);

	$effect(() => {
		if (needsHome === false) {
			dismissedKey = null;
			queuedMotors = [];
		}
	});

	// The server rejects concurrent motion with 409, so clicks queue up and the
	// motors home one at a time. A successful home republishes status without
	// that motor, which removes its button.
	async function didHome(motor: string): Promise<void> {
		if (homingMotor === motor || queuedMotors.includes(motor)) {
			return;
		}
		queuedMotors = [...queuedMotors, motor];
		if (homingMotor !== null) {
			return;
		}
		while (queuedMotors.length > 0) {
			const [next, ...rest] = queuedMotors;
			queuedMotors = rest;
			homingMotor = next;
			try {
				await homeMotor(next);
			} catch (err) {
				// Stop the queue so the operator sees the failure before more motion.
				postToast('error', `${err instanceof Error ? err.message : 'home failed'} (during ${next})`);
				queuedMotors = [];
			}
		}
		homingMotor = null;
	}

	function didCancel(): void {
		dismissedKey = motorsKey;
	}
</script>

{#if isVisible}
	<div class="needs-home-scrim"></div>
	<aside class="needs-home-alert" role="alert">
		<p class="needs-home-title">motor needs homing</p>
		<div class="needs-home-actions">
			<button type="button" onclick={didCancel} disabled={isBusy}>
				cancel
			</button>
			<div class="needs-home-spacer"></div>
			{#if motors.length > 0}
				<div class="needs-home-buttons">
					{#each motors as motor (motor)}
						<button
							type="button"
							onclick={() => void didHome(motor)}
							disabled={homingMotor === motor || queuedMotors.includes(motor)}
						>
							home {motor}
						</button>
					{/each}
				</div>
			{:else}
				<a class="needs-home-link" href={devicePath('/motors')}>open motors page</a>
			{/if}
		</div>
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
		align-items: flex-end;
		margin-top: 1.5em;
	}

	.needs-home-buttons {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 0.5em;
	}

	.needs-home-spacer {
		flex: 1;
	}

	.needs-home-actions button,
	.needs-home-buttons button,
	.needs-home-link {
		margin-top: 0;
	}
</style>
