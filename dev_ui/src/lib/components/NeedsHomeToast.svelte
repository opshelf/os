<script lang="ts">
	import { homeMotor } from '$lib/api';
	import { orderForHoming } from '$lib/motor-order';
	import { devicePath } from '$lib/nav';
	import { robotStatus } from '$lib/stores';
	import { createToastSource, postToast, type ToastAction } from '$lib/toasts';

	// The global /robot/status stream is the single source of truth: a motor
	// crash publishes a snapshot with needs_home + needs_home_axes immediately,
	// and a successful home clears the flag and republishes.
	const needsHome = $derived($robotStatus?.needs_home === true);
	const motors = $derived(needsHome ? orderForHoming($robotStatus?.needs_home_axes ?? []) : []);
	const reason = $derived($robotStatus?.needs_home_reason ?? '');
	const motorsKey = $derived(motors.length > 0 ? motors.join(':') : `reason:${reason}`);
	const message = $derived(motors.length === 1 ? 'A motor needs homing.' : 'Motors need homing.');

	// Each motor set is announced once, under its own queue key, and the queue remembers
	// a dismissal or an expiry while that key and wording are unchanged.
	const source = createToastSource('status');
	let homingMotor = $state<string | null>(null);
	let queuedMotors = $state<string[]>([]);
	const isHoming = $derived(homingMotor !== null || queuedMotors.length > 0);

	function motorLabel(motor: string): string {
		return motor.replace(/_/g, ' ');
	}

	// The server rejects concurrent motion with 409, so the motors home one at a time, in
	// the tool's safe home order. A successful home republishes status without that motor.
	async function didHome(requested: string[]): Promise<void> {
		const fresh = requested.filter((motor) => homingMotor !== motor && queuedMotors.includes(motor) === false);
		if (fresh.length === 0) {
			return;
		}
		queuedMotors = orderForHoming([...queuedMotors, ...fresh]);
		if (homingMotor !== null) {
			return;
		}
		while (queuedMotors.length > 0) {
			const [next, ...rest] = queuedMotors;
			queuedMotors = rest;
			homingMotor = next;
			let failure: string | null = null;
			try {
				const result = await homeMotor(next);
				if (result?.success !== true) {
					failure = '';
				}
			} catch (err) {
				failure = err instanceof Error ? err.message : '';
			}
			if (failure !== null) {
				// Stop the queue so the operator sees the failure before more motion. The
				// needs-home toast comes back with whatever is still unhomed, so a retry is one tap.
				const named = `Homing ${motorLabel(next)} failed.`;
				postToast('error', failure === '' ? named : `${named} ${failure}`);
				queuedMotors = [];
			}
		}
		homingMotor = null;
	}

	function toastActions(): ToastAction[] {
		if (motors.length === 0) {
			return [
				{
					label: 'open motors page',
					onClick: () => {
						location.href = devicePath('/motors');
					}
				}
			];
		}

		const perMotor = motors.map((motor) => ({
			label: `home ${motorLabel(motor)}`,
			onClick: () => didHome([motor])
		}));

		if (motors.length < 2) {
			return perMotor;
		}

		return [{ label: 'home all', onClick: () => didHome(motors) }, ...perMotor];
	}

	$effect(() => {
		if (needsHome === false) {
			queuedMotors = [];
		}
	});

	// While homing runs the toast steps aside: each home republishes status, and without
	// this the toast would pop back with the motors that are still waiting their turn.
	// Once the queue is done it returns only if something is still unhomed.
	$effect(() => {
		const shown = needsHome === true && isHoming === false;
		source.report(shown ? message : '', {
			key: shown ? `needs-home:${motorsKey}` : '',
			actions: shown ? toastActions() : undefined
		});
	});

	$effect(() => () => source.release());
</script>
