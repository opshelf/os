<script lang="ts">
	import { homeMotor, homeRobot } from '$lib/api';
	import { orderForHoming } from '$lib/motor-order';
	import { robotInit, robotStatus } from '$lib/stores';
	import { createToastSource, postToast, type ToastAction } from '$lib/toasts';

	// The global /robot/status stream is the single source of truth: a motor
	// crash publishes a snapshot with needs_home + needs_home_axes immediately,
	// and a successful home clears the flag and republishes.
	const needsHome = $derived($robotStatus?.needs_home === true);
	const motors = $derived(needsHome ? orderForHoming($robotStatus?.needs_home_axes ?? []) : []);
	const reason = $derived($robotStatus?.needs_home_reason ?? '');
	const motorsKey = $derived(motors.length > 0 ? motors.join(':') : `reason:${reason}`);
	const message = $derived(motors.length === 1 ? 'A motor needs homing.' : 'Motors need homing.');

	// Every motor home reports a homing phase on the init progress stream, whoever started it
	// (this toast, the motors page, a home or recover command, the kiosk), and ends it with
	// ready or failed.
	const trackerHoming = $derived(
		($robotInit?.components ?? [])
			.filter((component) => component.phase === 'homing')
			.map((component) => component.component)
	);
	const commandName = $derived(
		typeof $robotStatus?.current_command?.command === 'string' ? $robotStatus.current_command.command : null
	);
	const isHomeCommand = $derived(commandName === 'home' || commandName === 'recover');

	const needsHomeSource = createToastSource('status');
	const progressSource = createToastSource('status');
	// The last homing failure stays up until the next attempt starts or nothing needs homing,
	// so a red toast never outlives a successful retry.
	const failureSource = createToastSource('error');

	function reportFailure(motor: string, detail: string): void {
		const named = `Homing ${motorLabel(motor)} failed.`;
		failureSource.report(detail === '' ? named : `${named} ${detail}`, { key: `home-failed:${motor}` });
		hasFailed = true;
	}

	// This toast's own homes go one motor at a time through the debug endpoint: the server
	// rejects concurrent motion with 409. A successful home republishes status without that motor.
	let homingMotor = $state<string | null>(null);
	let queuedMotors = $state<string[]>([]);
	let batchTotal = $state(0);
	let batchDone = $state(0);
	const isQueueBusy = $derived(homingMotor !== null || queuedMotors.length > 0);

	// A home command is queued before it shows up as the current command; hold the prompt back
	// in between so it does not flash.
	let isCommandRequested = $state(false);
	let commandRequestTimer: ReturnType<typeof setTimeout> | null = null;

	// Once a home has failed the bulk action reads retry until the motors are homed.
	let hasFailed = $state(false);

	// Startup homing has its own progress view and no status yet, so only a running robot counts.
	const isRobotHoming = $derived($robotStatus != null && (isHomeCommand || trackerHoming.length > 0));
	const isHoming = $derived(isQueueBusy || isCommandRequested || isRobotHoming);

	function motorLabel(motor: string): string {
		return motor.replace(/_/g, ' ');
	}

	function progressMessage(): string {
		if (homingMotor !== null) {
			const count = batchTotal > 1 ? ` (${batchDone + 1} of ${batchTotal})` : '';
			return `Homing ${motorLabel(homingMotor)} motor${count}.`;
		}
		if (trackerHoming.length === 1) {
			return `Homing ${motorLabel(trackerHoming[0] as string)} motor.`;
		}
		if (trackerHoming.length > 1) {
			return `Homing ${trackerHoming.map(motorLabel).join(', ')} motors.`;
		}
		if (commandName === 'recover') {
			return 'Recovering the tool.';
		}
		return 'Homing motors.';
	}

	async function didHome(requested: string[]): Promise<void> {
		const fresh = requested.filter((motor) => homingMotor !== motor && queuedMotors.includes(motor) === false);
		if (fresh.length === 0) {
			return;
		}
		queuedMotors = orderForHoming([...queuedMotors, ...fresh]);
		if (homingMotor !== null) {
			batchTotal += fresh.length;
			return;
		}
		batchTotal = queuedMotors.length;
		batchDone = 0;
		failureSource.report('');
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
				// needs-home toast comes back with whatever is still unhomed.
				reportFailure(next, failure);
				queuedMotors = [];
			} else {
				batchDone += 1;
			}
		}
		homingMotor = null;
		batchTotal = 0;
		batchDone = 0;
	}

	// With no motors named, only a full home clears the flag.
	async function didHomeRobot(): Promise<void> {
		if (isCommandRequested === true) {
			return;
		}
		isCommandRequested = true;
		failureSource.report('');
		// The command shows up as current within a status publish; if it already came and went,
		// stop holding the prompt back after a while.
		commandRequestTimer = setTimeout(() => {
			isCommandRequested = false;
		}, 10000);
		try {
			await homeRobot();
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'The home command failed.');
			isCommandRequested = false;
		}
	}

	function toastActions(): ToastAction[] {
		const bulkLabel = hasFailed ? 'retry' : 'home all';

		if (motors.length === 0) {
			return [{ label: bulkLabel, onClick: () => didHomeRobot() }];
		}

		if (motors.length === 1 && hasFailed) {
			return [{ label: 'retry', onClick: () => didHome(motors) }];
		}

		const perMotor = motors.map((motor) => ({
			label: `home ${motorLabel(motor)}`,
			onClick: () => didHome([motor])
		}));

		if (motors.length < 2) {
			return perMotor;
		}

		return [{ label: bulkLabel, onClick: () => didHome(motors) }, ...perMotor];
	}

	$effect(() => {
		if (needsHome === false) {
			queuedMotors = [];
			hasFailed = false;
			failureSource.report('');
		}
	});

	// The request hold ends once the command is running; from then on the current command
	// keeps the prompt back until it is done.
	$effect(() => {
		if (isHomeCommand === true && isCommandRequested === true) {
			isCommandRequested = false;
			if (commandRequestTimer !== null) {
				clearTimeout(commandRequestTimer);
				commandRequestTimer = null;
			}
		}
	});

	// A home command fails on the worker, not in a response, so its failure is read off the
	// init progress stream: a motor that goes from homing to failed while the command runs.
	// The toast's own homes report their failure from the response instead.
	let previousPhases = new Map<string, string>();
	$effect(() => {
		const components = $robotInit?.components ?? [];
		const isCommandHoming = isHomeCommand || isCommandRequested;
		const isOwnHoming = isQueueBusy;
		for (const component of components) {
			const before = previousPhases.get(component.component);
			if (
				component.phase === 'failed' &&
				before === 'homing' &&
				isCommandHoming === true &&
				isOwnHoming === false
			) {
				reportFailure(component.component, component.reason ?? '');
			}
		}
		previousPhases = new Map(components.map((component) => [component.component, component.phase]));
	});

	// While any homing runs the prompt steps aside for a progress toast; each home republishes
	// status, and the prompt would otherwise pop back with the motors still waiting their turn.
	// Once homing is done it returns only if something is still unhomed. Both stay up until
	// their condition is over.
	$effect(() => {
		const shown = needsHome === true && isHoming === false;
		needsHomeSource.report(shown ? message : '', {
			key: shown ? `needs-home:${motorsKey}` : '',
			actions: shown ? toastActions() : undefined,
			persistent: true
		});
	});

	$effect(() => {
		const text = isHoming === true ? progressMessage() : '';
		progressSource.report(text, { key: isHoming === true ? 'homing-progress' : '', persistent: true });
	});

	$effect(() => () => {
		needsHomeSource.release();
		progressSource.release();
		failureSource.release();
		if (commandRequestTimer !== null) {
			clearTimeout(commandRequestTimer);
		}
	});
</script>
