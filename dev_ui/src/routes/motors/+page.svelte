<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getMotors,
		simulateMotorCrash,
		clearMotorAlarm,
		homeMotor,
		controlGripper,
		controlDoor,
		moveMotor,
		probeMotors,
		getMotorLimitSwitches,
		getMotorLimitSwitch,
		apiFetch,
		type MotorList,
		type MotorLimitSwitchState,
		type MotorInfo,
		type GripperState,
		type DoorAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';
	import { orderSectionMoves, type MotorMove } from '$lib/motor-order';
	import { robotStatus } from '$lib/stores';
	import { createToastSource, postToast } from '$lib/toasts';

	let motors: MotorList | null = null;
	let isLoading = true;
	let activeMotor: string | null = null;
	let gripperAction: GripperState | null = null;
	let doorAction: DoorAction | null = null;
	let error = '';
	let message = '';

	let testResults: Record<string, 'ok' | 'err'> = {};
	let isTesting = false;
	let testingMotor: string | null = null;
	let isHomingAll = false;
	let moveTargets: Record<string, string> = {};
	let movingMotor: string | null = null;
	let isProbing = false;
	let sectionAction: string | null = null;
	let motorPositions: Record<string, string> = {};
	let limitSwitches: Record<string, MotorLimitSwitchState> = {};
	// The poll repeats every second, so a failing probe is one condition, not a toast per tick.
	const pollState = createToastSource('error');

	const POSITION_POLL_INTERVAL_MS = 1000;

	let activeAction: 'crash' | 'home' | 'clear' | null = null;

	function unitForMotor(motorName: string): string {
		if (motorName === 'rotate') {
			return 'deg';
		}

		return 'mm';
	}

	function motorLabel(motorName: string): string {
		return motorName.replace(/_/g, ' ');
	}

	function sectionLabel(sectionName: string): string {
		return sectionName === 'io' ? 'IO' : sectionName;
	}

	function positionLabelForMotor(motorName: string, position: string | undefined): string {
		if (position === undefined) {
			return '-';
		}

		return `${position} ${unitForMotor(motorName)}`;
	}

	function minLimitLabel(state: MotorLimitSwitchState | undefined): string {
		if (state === undefined || state.enabled === false) {
			return '-';
		}

		if (state.min_engaged === null) {
			return '?';
		}

		return state.min_engaged === true ? 'H' : 'L';
	}

	function gantryMotors(): MotorInfo[] {
		if (motors === null) {
			return [];
		}

		const toolMotors = motors.tool;

		return ['x_axis', 'y_axis']
			.map((motorName) => toolMotors.find((motor) => motor.name === motorName))
			.filter((motor): motor is MotorInfo => motor !== undefined);
	}

	function toolMotors(): MotorInfo[] {
		if (motors === null) {
			return [];
		}

		return motors.tool.filter((motor) => motor.name !== 'x_axis' && motor.name !== 'y_axis');
	}

	function ioMotors(): MotorInfo[] {
		if (motors === null) {
			return [];
		}

		return motors.io.filter((motor) => motor.name !== 'fridge_door');
	}

	function fridgeMotors(): MotorInfo[] {
		if (motors === null) {
			return [];
		}

		return motors.io.filter((motor) => motor.name === 'fridge_door');
	}

	function isActionDisabled(): boolean {
		return (
			activeMotor !== null ||
			movingMotor !== null ||
			gripperAction !== null ||
			doorAction !== null ||
			isTesting === true ||
			isHomingAll === true ||
			sectionAction !== null
		);
	}

	async function fetchMotors(): Promise<void> {
		try {
			motors = await getMotors();
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load motors.';
		}
	}

	async function fetchPositions(): Promise<void> {
		if (isProbing === true) {
			return;
		}

		if (browser === true && document.visibilityState !== 'visible') {
			return;
		}

		isProbing = true;
		try {
			const result = await probeMotors();

			if (result.busy === true) {
				return;
			}
			pollState.report('');

			const nextPositions = { ...motorPositions };
			const nextTargets = { ...moveTargets };

			for (const r of result.results) {
				if (r.responsive === true && r.position_mm !== null) {
					const position = r.position_mm.toFixed(2);
					nextPositions[r.motor_name] = position;

					if (nextTargets[r.motor_name] === undefined) {
						nextTargets[r.motor_name] = position;
					}
				}
			}

			motorPositions = nextPositions;
			moveTargets = nextTargets;
		} catch (err) {
			pollState.report(err instanceof Error ? err.message : 'The motor positions could not be read.');
		} finally {
			isProbing = false;
		}
	}

	async function fetchLimitSwitches(): Promise<void> {
		if (browser === true && document.visibilityState !== 'visible') {
			return;
		}

		try {
			const result = await getMotorLimitSwitches();
			const next: Record<string, MotorLimitSwitchState> = {};

			for (const state of result.results) {
				next[state.motor_name] = state;
			}

			limitSwitches = next;
		} catch {
			limitSwitches = {};
		}
	}

	// A motor without an enabled min limit switch has nothing to check.
	async function expectMinLimit(motorName: string, isHomed: boolean): Promise<void> {
		const state = await getMotorLimitSwitch(motorName);
		limitSwitches = { ...limitSwitches, [motorName]: state };

		if (state.enabled === false) {
			return;
		}

		if (state.min_engaged === null) {
			throw new Error(`The ${motorLabel(motorName)} min limit switch could not be read.`);
		}

		if (state.min_engaged !== isHomed) {
			const where = isHomed === true ? 'after homing' : 'away from home';
			const expected = isHomed === true ? 'H' : 'L';
			throw new Error(
				`The ${motorLabel(motorName)} min limit switch reads ${minLimitLabel(state)} ${where}, expected ${expected}.`
			);
		}
	}

	async function didClickCrash(motorName: string): Promise<void> {
		activeMotor = motorName;
		activeAction = 'crash';
		error = '';
		message = '';

		try {
			const result = await simulateMotorCrash(motorName);

			if (result.success === true) {
				message = `Crash simulated for ${motorLabel(motorName)}.`;
			} else {
				error = `Failed to simulate crash for ${motorLabel(motorName)}.`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `Crash failed for ${motorLabel(motorName)}.`;
		} finally {
			activeMotor = null;
			activeAction = null;
		}
	}

	async function didClickClear(motorName: string): Promise<void> {
		activeMotor = motorName;
		activeAction = 'clear';
		error = '';
		message = '';

		try {
			const result = await clearMotorAlarm(motorName);

			if (result.success === true) {
				message = `Alarm cleared for ${motorLabel(motorName)}.`;
			} else {
				error = `Failed to clear alarm for ${motorLabel(motorName)}.`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `Clear alarm failed for ${motorLabel(motorName)}.`;
		} finally {
			activeMotor = null;
			activeAction = null;
		}
	}

	async function didClickHome(motorName: string): Promise<void> {
		activeMotor = motorName;
		activeAction = 'home';
		error = '';
		message = '';

		try {
			const result = await homeMotor(motorName);

			if (result.success === true) {
				message = `Homed ${motorLabel(motorName)}.`;
			} else {
				error = `Failed to home ${motorLabel(motorName)}.`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `Home failed for ${motorLabel(motorName)}.`;
		} finally {
			activeMotor = null;
			activeAction = null;
		}
	}

	async function didClickGripper(state: GripperState): Promise<void> {
		gripperAction = state;
		error = '';
		message = '';

		try {
			await controlGripper(state);
			message = `Gripper ${state}.`;
		} catch (err) {
			error = err instanceof Error ? err.message : `Gripper ${state} failed.`;
		} finally {
			gripperAction = null;
		}
	}

	async function didClickDoor(action: DoorAction): Promise<void> {
		doorAction = action;
		error = '';
		message = '';

		try {
			await controlDoor(action);
			message = `Door ${action} complete.`;
		} catch (err) {
			error = err instanceof Error ? err.message : `Door ${action} failed.`;
		} finally {
			doorAction = null;
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchMotors();
			await Promise.all([fetchPositions(), fetchLimitSwitches()]);
		} finally {
			isLoading = false;
		}
	}

	async function testSingleMotor(motorName: string): Promise<void> {
		testingMotor = motorName;

		try {
			// Far enough to release the min switch; on rotate it stays engaged past 3 deg.
			await moveMotor(motorName, 10);
			await expectMinLimit(motorName, false);
			await homeMotor(motorName);
			await expectMinLimit(motorName, true);
			testResults[motorName] = 'ok';
		} catch (err) {
			testResults[motorName] = 'err';
			postToast('error', err instanceof Error ? err.message : `The ${motorLabel(motorName)} test failed.`);
		}
	}

	async function testGripper(): Promise<void> {
		testingMotor = 'gripper';

		try {
			await controlGripper('grip');
			await controlGripper('release');
			testResults['gripper'] = 'ok';
		} catch {
			testResults['gripper'] = 'err';
		}
	}

	function clearTestResult(motorName: string): void {
		const nextResults = { ...testResults };
		delete nextResults[motorName];
		testResults = nextResults;
	}

	async function didClickTestMotor(motorName: string): Promise<void> {
		isTesting = true;
		clearTestResult(motorName);
		error = '';
		message = '';

		try {
			await testSingleMotor(motorName);
			message = `The ${motorLabel(motorName)} test is complete.`;
		} finally {
			testingMotor = null;
			isTesting = false;
		}
	}

	async function didClickTestGripper(): Promise<void> {
		isTesting = true;
		clearTestResult('gripper');
		error = '';
		message = '';

		try {
			await testGripper();
			message = 'Gripper test complete.';
		} finally {
			testingMotor = null;
			isTesting = false;
		}
	}

	async function didClickTestAll(): Promise<void> {
		if (motors === null) {
			return;
		}

		isTesting = true;
		testResults = {};
		error = '';
		message = '';

		const allMotors = [...motors.io, ...motors.tool];

		for (const motor of allMotors) {
			await testSingleMotor(motor.name);
		}

		await testGripper();

		testingMotor = null;
		isTesting = false;
		message = 'Test complete.';
	}

	async function didClickHomeAll(): Promise<void> {
		isHomingAll = true;
		error = '';
		message = '';

		try {
			await apiFetch('/robot/commands', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ command: 'home' })
			});
			message = 'Home command sent.';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Home command failed.';
		} finally {
			isHomingAll = false;
		}
	}

	async function didClickHomeSection(
		sectionName: string,
		sectionMotors: MotorInfo[]
	): Promise<void> {
		sectionAction = `${sectionName}:home`;
		error = '';
		message = '';

		try {
			for (const motor of sectionMotors) {
				activeMotor = motor.name;
				activeAction = 'home';
				const result = await homeMotor(motor.name);

				if (result.success !== true) {
					error = `Failed to home ${motorLabel(motor.name)}.`;
					return;
				}
			}

			message = `Homed the ${sectionLabel(sectionName)} motors.`;
		} catch (err) {
			error = err instanceof Error ? err.message : `Homing the ${sectionLabel(sectionName)} motors failed.`;
		} finally {
			activeMotor = null;
			activeAction = null;
			sectionAction = null;
		}
	}

	// Recover opens the fingers and withdraws the tool without homing, then homes only the axes
	// the robot reports as having lost position.
	async function didClickRecoverTool(): Promise<void> {
		sectionAction = 'tool:recover';
		error = '';
		message = '';
		const axes = $robotStatus?.needs_home_axes ?? [];

		try {
			await apiFetch('/robot/commands', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(axes.length > 0 ? { command: 'recover', axes } : { command: 'recover' })
			});
			message = 'Recover command sent.';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Recover command failed.';
		} finally {
			sectionAction = null;
		}
	}

	async function didClickMoveSection(
		sectionName: string,
		sectionMotors: MotorInfo[]
	): Promise<void> {
		const targets: MotorMove[] = [];

		for (const motor of sectionMotors) {
			const targetValue = moveTargets[motor.name];

			if (targetValue === undefined || targetValue === '') {
				error = `Enter a position for ${motorLabel(motor.name)}.`;
				return;
			}

			const positionMm = parseFloat(targetValue);

			if (Number.isNaN(positionMm) === true) {
				error = `Invalid position value for ${motorLabel(motor.name)}.`;
				return;
			}

			targets.push({ name: motor.name, positionMm });
		}

		const currentMm: Record<string, number | undefined> = {};

		for (const [name, position] of Object.entries(motorPositions)) {
			currentMm[name] = parseFloat(position);
		}

		const ordered = orderSectionMoves(targets, currentMm);

		if ('unknown' in ordered) {
			error = `The ${motorLabel(ordered.unknown)} position is unknown. Wait for the position readout.`;
			return;
		}

		sectionAction = `${sectionName}:move`;
		error = '';
		message = '';

		try {
			for (const target of ordered) {
				movingMotor = target.name;
				const result = await moveMotor(target.name, target.positionMm);

				if (result.success !== true) {
					error = `Failed to move ${motorLabel(target.name)}.`;
					return;
				}
			}

			message = `Moved the ${sectionLabel(sectionName)} motors.`;
		} catch (err) {
			error = err instanceof Error ? err.message : `Moving the ${sectionLabel(sectionName)} motors failed.`;
		} finally {
			movingMotor = null;
			sectionAction = null;
		}
	}

	async function didClickMove(motorName: string): Promise<void> {
		const targetValue = moveTargets[motorName];

		if (targetValue === undefined || targetValue === '') {
			error = `Enter a position for ${motorLabel(motorName)}.`;
			return;
		}

		const positionMm = parseFloat(targetValue);

		if (Number.isNaN(positionMm) === true) {
			error = `Invalid position value for ${motorLabel(motorName)}.`;
			return;
		}

		movingMotor = motorName;
		error = '';
		message = '';

		try {
			const result = await moveMotor(motorName, positionMm);

			if (result.success === true) {
				message = `Moved ${motorLabel(motorName)} to ${positionMm} ${unitForMotor(motorName)}.`;
			} else {
				error = `Failed to move ${motorLabel(motorName)}.`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `Move failed for ${motorLabel(motorName)}.`;
		} finally {
			movingMotor = null;
		}
	}

	function didInputTarget(motorName: string, event: Event): void {
		const target = event.target as HTMLInputElement;
		moveTargets[motorName] = target.value;
	}

	onMount(function () {
		if (browser === false) {
			return;
		}

		void initialLoad();

		const didChangeVisibility = (): void => {
			if (document.visibilityState === 'visible') {
				void fetchPositions();
				void fetchLimitSwitches();
			}
		};

		const interval = setInterval(() => {
			void fetchPositions();
			void fetchLimitSwitches();
		}, POSITION_POLL_INTERVAL_MS);

		document.addEventListener('visibilitychange', didChangeVisibility);

		return () => {
			clearInterval(interval);
			document.removeEventListener('visibilitychange', didChangeVisibility);
			pollState.release();
		};
	});
</script>

<svelte:head>
	<title>Motors • Openshelf</title>
</svelte:head>

{#if isLoading === true}
	<p>loading motors...</p>
{:else if motors !== null}
	<div class="actions">
		<button
			type="button"
			on:click={didClickTestAll}
			disabled={isTesting === true ||
				activeMotor !== null ||
				movingMotor !== null ||
				isHomingAll === true}
		>
			TEST ALL
		</button>
		<button
			type="button"
			on:click={didClickHomeAll}
			disabled={isHomingAll === true ||
				activeMotor !== null ||
				movingMotor !== null ||
				isTesting === true}
		>
			HOME ALL
		</button>
	</div>

	<div class="scroll-x motor-table-scroll">
		<table class="motor-table">
			<thead>
				<tr>
					<th colspan="7">
						<div class="section-heading">
							<span>gantry motors</span>
							<span>
								<button
									type="button"
									on:click={() => didClickHomeSection('gantry', gantryMotors())}
									disabled={isActionDisabled()}>HOME</button
								>
								<button
									type="button"
									on:click={() => didClickMoveSection('gantry', gantryMotors())}
									disabled={isActionDisabled()}>MOVE</button
								>
							</span>
						</div>
					</th>
				</tr>
				<tr>
					<th>id</th>
					<th>motor</th>
					<th>status</th>
					<th>min</th>
					<th>POSITION</th>
					<th>move to</th>
					<th>actions</th>
				</tr>
			</thead>
			<tbody>
				{#each gantryMotors() as motor (motor.name)}
					<tr>
						<td class="motor-id">{motor.motor_id}</td>
						<td>{motor.name}</td>
						<td>
							{#if testingMotor === motor.name}
								<span class="status-testing">TEST</span>
							{:else if testResults[motor.name] === 'ok'}
								<span class="status-ok">OK</span>
							{:else if testResults[motor.name] === 'err'}
								<span class="status-error">ERR</span>
							{:else}
								<span class="status-unknown">-</span>
							{/if}
						</td>
						<td>{minLimitLabel(limitSwitches[motor.name])}</td>
						<td>{positionLabelForMotor(motor.name, motorPositions[motor.name])}</td>
						<td>
							<input
								type="number"
								step="0.1"
								placeholder={unitForMotor(motor.name)}
								value={moveTargets[motor.name] ?? ''}
								on:input={(e) => didInputTarget(motor.name, e)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							/>
							<button
								type="button"
								on:click={() => didClickMove(motor.name)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							>
								go
							</button>
						</td>
						<td>
							<button
								type="button"
								on:click={() => didClickTestMotor(motor.name)}
								disabled={isActionDisabled()}
							>
								TEST
							</button>
							<button
								type="button"
								on:click={() => didClickHome(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									doorAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								home
							</button>
							<button
								type="button"
								on:click={() => didClickCrash(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									doorAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								crash
							</button>
							<button
								type="button"
								on:click={() => didClickClear(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									doorAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								clear
							</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	<div class="scroll-x motor-table-scroll">
		<table class="motor-table">
			<thead>
				<tr>
					<th colspan="7">
						<div class="section-heading">
							<span>tool motors</span>
							<span>
								<button
									type="button"
									on:click={() => didClickHomeSection('tool', toolMotors())}
									disabled={isActionDisabled()}>HOME</button
								>
								<button
									type="button"
									on:click={() => didClickMoveSection('tool', toolMotors())}
									disabled={isActionDisabled()}>MOVE</button
								>
								<button
									type="button"
									on:click={didClickRecoverTool}
									disabled={isActionDisabled()}>RECOVER</button
								>
							</span>
						</div>
					</th>
				</tr>
				<tr>
					<th>id</th>
					<th>motor</th>
					<th>status</th>
					<th>min</th>
					<th>POSITION</th>
					<th>move to</th>
					<th>actions</th>
				</tr>
			</thead>
			<tbody>
				{#each toolMotors() as motor (motor.name)}
					<tr>
						<td class="motor-id">{motor.motor_id}</td>
						<td>{motor.name}</td>
						<td>
							{#if testingMotor === motor.name}
								<span class="status-testing">TEST</span>
							{:else if testResults[motor.name] === 'ok'}
								<span class="status-ok">OK</span>
							{:else if testResults[motor.name] === 'err'}
								<span class="status-error">ERR</span>
							{:else}
								<span class="status-unknown">-</span>
							{/if}
						</td>
						<td>{minLimitLabel(limitSwitches[motor.name])}</td>
						<td>{positionLabelForMotor(motor.name, motorPositions[motor.name])}</td>
						<td>
							<input
								type="number"
								step="0.1"
								placeholder={unitForMotor(motor.name)}
								value={moveTargets[motor.name] ?? ''}
								on:input={(e) => didInputTarget(motor.name, e)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							/>
							<button
								type="button"
								on:click={() => didClickMove(motor.name)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							>
								go
							</button>
						</td>
						<td>
							<button
								type="button"
								on:click={() => didClickTestMotor(motor.name)}
								disabled={isActionDisabled()}
							>
								TEST
							</button>
							<button
								type="button"
								on:click={() => didClickHome(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								home
							</button>
							<button
								type="button"
								on:click={() => didClickCrash(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								crash
							</button>
							<button
								type="button"
								on:click={() => didClickClear(motor.name)}
								disabled={activeMotor !== null ||
									gripperAction !== null ||
									movingMotor !== null ||
									isTesting === true}
							>
								clear
							</button>
						</td>
					</tr>
				{/each}
				<tr>
					<td class="motor-id">9</td>
					<td>gripper</td>
					<td>
						{#if testingMotor === 'gripper'}
							<span class="status-testing">TEST</span>
						{:else if testResults['gripper'] === 'ok'}
							<span class="status-ok">OK</span>
						{:else if testResults['gripper'] === 'err'}
							<span class="status-error">ERR</span>
						{:else}
							<span class="status-unknown">-</span>
						{/if}
					</td>
					<td>-</td>
					<td>-</td>
					<td>-</td>
					<td>
						<button type="button" on:click={didClickTestGripper} disabled={isActionDisabled()}>
							TEST
						</button>
						<button
							type="button"
							on:click={() => didClickGripper('grip')}
							disabled={gripperAction !== null ||
								activeMotor !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							grip
						</button>
						<button
							type="button"
							on:click={() => didClickGripper('release')}
							disabled={gripperAction !== null ||
								activeMotor !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							release
						</button>
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<div class="scroll-x motor-table-scroll">
		<table class="motor-table">
			<thead>
				<tr>
					<th colspan="7">
						<div class="section-heading">
							<span>io motors</span>
							<span>
								<button
									type="button"
									on:click={() => didClickHomeSection('io', ioMotors())}
									disabled={isActionDisabled()}>HOME</button
								>
								<button
									type="button"
									on:click={() => didClickMoveSection('io', ioMotors())}
									disabled={isActionDisabled()}>MOVE</button
								>
							</span>
						</div>
					</th>
				</tr>
				<tr>
					<th>id</th>
					<th>motor</th>
					<th>status</th>
					<th>min</th>
					<th>POSITION</th>
					<th>move to</th>
				</tr>
			</thead>
			<tbody>
				{#each ioMotors() as motor (motor.name)}
					<tr>
						<td class="motor-id">{motor.motor_id}</td>
						<td>{motor.name}</td>
						<td>
							{#if testingMotor === motor.name}
								<span class="status-testing">TEST</span>
							{:else if testResults[motor.name] === 'ok'}
								<span class="status-ok">OK</span>
							{:else if testResults[motor.name] === 'err'}
								<span class="status-error">ERR</span>
							{:else}
								<span class="status-unknown">-</span>
							{/if}
						</td>
						<td>{minLimitLabel(limitSwitches[motor.name])}</td>
						<td>{positionLabelForMotor(motor.name, motorPositions[motor.name])}</td>
						<td>
							<input
								type="number"
								step="0.1"
								placeholder="mm"
								value={moveTargets[motor.name] ?? ''}
								on:input={(e) => didInputTarget(motor.name, e)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							/>
							<button
								type="button"
								on:click={() => didClickMove(motor.name)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							>
								go
							</button>
						</td>
						{#if motor.name !== 'door'}
							<td>
								<button
									type="button"
									on:click={() => didClickTestMotor(motor.name)}
									disabled={isActionDisabled()}>TEST</button
								>
								<button
									type="button"
									on:click={() => didClickHome(motor.name)}
									disabled={activeMotor !== null ||
										doorAction !== null ||
										movingMotor !== null ||
										isTesting === true}
								>
									home
								</button>
								<button
									type="button"
									on:click={() => didClickCrash(motor.name)}
									disabled={activeMotor !== null ||
										doorAction !== null ||
										movingMotor !== null ||
										isTesting === true}
								>
									crash
								</button>
								<button
									type="button"
									on:click={() => didClickClear(motor.name)}
									disabled={activeMotor !== null ||
										doorAction !== null ||
										movingMotor !== null ||
										isTesting === true}
								>
									clear
								</button>
							</td>
						{/if}
					</tr>
					{#if motor.name === 'door'}
						<tr>
							<td colspan="6">
								<div class="door-actions">
									<button
										type="button"
										on:click={() => didClickTestMotor('door')}
										disabled={isActionDisabled()}>TEST</button
									>
									<button
										type="button"
										on:click={() => didClickHome('door')}
										disabled={activeMotor !== null ||
											doorAction !== null ||
											movingMotor !== null ||
											isTesting === true}>home</button
									>
									<button
										type="button"
										on:click={() => didClickCrash('door')}
										disabled={activeMotor !== null ||
											doorAction !== null ||
											movingMotor !== null ||
											isTesting === true}>crash</button
									>
									<button
										type="button"
										on:click={() => didClickClear('door')}
										disabled={activeMotor !== null ||
											doorAction !== null ||
											movingMotor !== null ||
											isTesting === true}>clear</button
									>
									<button
										type="button"
										on:click={() => didClickDoor('open')}
										disabled={doorAction !== null ||
											activeMotor !== null ||
											movingMotor !== null ||
											isTesting === true}>open</button
									>
									<button
										type="button"
										on:click={() => didClickDoor('close')}
										disabled={doorAction !== null ||
											activeMotor !== null ||
											movingMotor !== null ||
											isTesting === true}>close</button
									>
								</div>
							</td>
						</tr>
					{/if}
				{/each}
			</tbody>
		</table>
	</div>

	{#if fridgeMotors().length > 0}
		<table class="motor-table">
			<thead>
				<tr>
					<th colspan="6">
						<div class="section-heading">
							<span>FRIDGE MOTORS</span>
							<span>
								<button
									type="button"
									on:click={() => didClickHomeSection('fridge', fridgeMotors())}
									disabled={isActionDisabled()}>HOME</button
								>
								<button
									type="button"
									on:click={() => didClickMoveSection('fridge', fridgeMotors())}
									disabled={isActionDisabled()}>MOVE</button
								>
							</span>
						</div>
					</th>
				</tr>
				<tr>
					<th>id</th>
					<th class="motor-name">motor</th>
					<th>status</th>
					<th class="position-column">POSITION</th>
					<th>move to</th>
					<th>actions</th>
				</tr>
			</thead>
			<tbody>
				{#each fridgeMotors() as motor (motor.name)}
					<tr>
						<td class="motor-id">{motor.motor_id}</td>
						<td class="motor-name">{motor.name}</td>
						<td>
							{#if testingMotor === motor.name}
								<span class="status-testing">TEST</span>
							{:else if testResults[motor.name] === 'ok'}
								<span class="status-ok">OK</span>
							{:else if testResults[motor.name] === 'err'}
								<span class="status-error">ERR</span>
							{:else}
								<span class="status-unknown">-</span>
							{/if}
						</td>
						<td class="position-column"
							>{positionLabelForMotor(motor.name, motorPositions[motor.name])}</td
						>
						<td>
							<input
								type="number"
								step="0.1"
								placeholder={unitForMotor(motor.name)}
								value={moveTargets[motor.name] ?? ''}
								on:input={(e) => didInputTarget(motor.name, e)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							/>
							<button
								type="button"
								on:click={() => didClickMove(motor.name)}
								disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
							>
								go
							</button>
						</td>
						<td>
							<button
								type="button"
								on:click={() => didClickTestMotor(motor.name)}
								disabled={isActionDisabled()}>TEST</button
							>
							<button
								type="button"
								on:click={() => didClickHome(motor.name)}
								disabled={isActionDisabled()}>home</button
							>
							<button
								type="button"
								on:click={() => didClickCrash(motor.name)}
								disabled={isActionDisabled()}>crash</button
							>
							<button
								type="button"
								on:click={() => didClickClear(motor.name)}
								disabled={isActionDisabled()}>clear</button
							>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{:else if error !== ''}
	<ErrorMessage message={error} />
{/if}

<style>
	.actions {
		margin-bottom: 1.5rem;
		gap: 1em;
		display: flex;
	}

	td {
		white-space: nowrap;
	}

	.motor-table-scroll + .motor-table-scroll {
		margin-top: 1.5em;
	}

	.section-heading {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.section-heading span:last-child {
		display: flex;
		gap: 0.5rem;
	}

	.door-actions {
		display: flex;
		gap: 0.5rem;
	}

	button {
		margin-top: 0;
	}

	input[type='number'] {
		width: 80px;
		margin-right: 4px;
	}

	.motor-id {
		text-align: center;
		color: #666;
		font-variant-numeric: tabular-nums;
	}

	.status-ok {
		color: green;
	}

	.status-error {
		color: red;
	}

	.status-unknown {
		color: gray;
	}

	.status-testing {
		color: orange;
	}

</style>
