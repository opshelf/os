<script lang="ts">
	import { homeMotor } from '$lib/api';
	import { devicePath } from '$lib/nav';
	import { robotStatus } from '$lib/stores';
	import { dismissToast, postToast, updateToastActions, type ToastAction } from '$lib/toasts';

	// The global /robot/status stream is the single source of truth: a motor
	// crash publishes a snapshot with needs_home + needs_home_axes immediately,
	// and a successful home clears the flag and republishes.
	const needsHome = $derived($robotStatus?.needs_home === true);
	const motors = $derived(needsHome ? [...($robotStatus?.needs_home_axes ?? [])].sort() : []);
	const reason = $derived($robotStatus?.needs_home_reason ?? '');
	const motorsKey = $derived(motors.length > 0 ? motors.join(':') : `reason:${reason}`);
	const message = $derived(motors.length === 1 ? 'motor needs homing' : 'motors need homing');

	// Each motor set is announced once. The id remains after timed expiry so
	// repeated status snapshots do not recreate the same toast.
	let shownKey = $state<string | null>(null);
	let toastId = $state<number | null>(null);
	let homingMotor = $state<string | null>(null);
	let queuedMotors = $state<string[]>([]);

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
				postToast(
					'error',
					`${err instanceof Error ? err.message : 'home failed'} (during ${next})`
				);
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

		return motors.map((motor) => ({
			label: `home ${motor}`,
			onClick: () => didHome(motor),
			disabled: homingMotor === motor || queuedMotors.includes(motor)
		}));
	}

	$effect(() => {
		const actions = toastActions();

		if (needsHome === false) {
			if (toastId !== null) {
				dismissToast(toastId);
			}
			shownKey = null;
			toastId = null;
			queuedMotors = [];
			return;
		}

		if (shownKey !== motorsKey) {
			if (toastId !== null) {
				dismissToast(toastId);
			}
			shownKey = motorsKey;
			toastId = postToast('status', message, actions);
			return;
		}

		if (toastId !== null) {
			updateToastActions(toastId, actions);
		}
	});
</script>
