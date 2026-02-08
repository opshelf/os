<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getMotors,
		simulateMotorCrash,
		homeMotor,
		controlGripper,
		controlDoor,
		moveMotor,
		apiFetch,
		type MotorList,
		type GripperState,
		type DoorAction
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let motors: MotorList | null = null;
	let isLoading = true;
	let activeMotor: string | null = null;
	let activeAction: string | null = null;
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

	function unitForMotor(motorName: string): string {
		if (motorName === 'rotate') {
			return 'deg';
		}

		return 'mm';
	}

	async function fetchMotors(): Promise<void> {
		try {
			motors = await getMotors();
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load motors';
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
				message = `crash simulated for ${motorName}`;
			} else {
				error = `failed to simulate crash for ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `crash failed for ${motorName}`;
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
				message = `${motorName} homed`;
			} else {
				error = `failed to home ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `home failed for ${motorName}`;
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
			message = `gripper ${state}`;
		} catch (err) {
			error = err instanceof Error ? err.message : `gripper ${state} failed`;
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
			message = `door ${action} complete`;
		} catch (err) {
			error = err instanceof Error ? err.message : `door ${action} failed`;
		} finally {
			doorAction = null;
		}
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;

		try {
			await fetchMotors();
		} finally {
			isLoading = false;
		}
	}

	async function testSingleMotor(motorName: string): Promise<void> {
		testingMotor = motorName;

		try {
			const testDistance = motorName === 'rotate' ? 1 : 10;

			await moveMotor(motorName, testDistance);
			await homeMotor(motorName);
			testResults[motorName] = 'ok';
		} catch {
			testResults[motorName] = 'err';
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
			await testSingleMotor(motor);
		}

		await testGripper();

		testingMotor = null;
		isTesting = false;
		message = 'test complete';
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
			message = 'home command sent';
		} catch (err) {
			error = err instanceof Error ? err.message : 'home command failed';
		} finally {
			isHomingAll = false;
		}
	}

	async function didClickMove(motorName: string): Promise<void> {
		const targetValue = moveTargets[motorName];

		if (targetValue === undefined || targetValue === '') {
			error = `enter a position for ${motorName}`;
			return;
		}

		const positionMm = parseFloat(targetValue);

		if (Number.isNaN(positionMm) === true) {
			error = `invalid position value for ${motorName}`;
			return;
		}

		movingMotor = motorName;
		error = '';
		message = '';

		try {
			const result = await moveMotor(motorName, positionMm);

			if (result.success === true) {
				message = `${motorName} moved to ${positionMm}${unitForMotor(motorName)}`;
			} else {
				error = `failed to move ${motorName}`;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : `move failed for ${motorName}`;
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

		initialLoad();
	});
</script>

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
			{isTesting === true ? 'testing...' : 'test all motors'}
		</button>
		<button
			type="button"
			on:click={didClickHomeAll}
			disabled={isHomingAll === true ||
				activeMotor !== null ||
				movingMotor !== null ||
				isTesting === true}
		>
			{isHomingAll === true ? 'homing...' : 'home all motors'}
		</button>
	</div>

	<table>
		<thead>
			<tr>
				<th colspan="4">io motors</th>
			</tr>
			<tr>
				<th>motor</th>
				<th>status</th>
				<th>move to</th>
				<th>actions</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.io.filter((m) => m !== 'door') as motor}
				<tr>
					<td>{motor}</td>
					<td>
						{#if testingMotor === motor}
							<span class="status-testing">TEST</span>
						{:else if testResults[motor] === 'ok'}
							<span class="status-ok">OK</span>
						{:else if testResults[motor] === 'err'}
							<span class="status-error">ERR</span>
						{:else}
							<span class="status-unknown">-</span>
						{/if}
					</td>
					<td>
						<input
							type="number"
							step="0.1"
							placeholder={unitForMotor(motor)}
							value={moveTargets[motor] ?? ''}
							on:input={(e) => didInputTarget(motor, e)}
							disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
						/>
						<button
							type="button"
							on:click={() => didClickMove(motor)}
							disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
						>
							{movingMotor === motor ? '...' : 'go'}
						</button>
					</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null ||
								doorAction !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							home
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null ||
								doorAction !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							crash
						</button>
					</td>
				</tr>
			{/each}
			<tr>
				<td>door</td>
				<td>
					{#if testingMotor === 'door'}
						<span class="status-testing">TEST</span>
					{:else if testResults['door'] === 'ok'}
						<span class="status-ok">OK</span>
					{:else if testResults['door'] === 'err'}
						<span class="status-error">ERR</span>
					{:else}
						<span class="status-unknown">-</span>
					{/if}
				</td>
				<td>
					<input
						type="number"
						step="0.1"
						placeholder="mm"
						value={moveTargets['door'] ?? ''}
						on:input={(e) => didInputTarget('door', e)}
						disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
					/>
					<button
						type="button"
						on:click={() => didClickMove('door')}
						disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
					>
						{movingMotor === 'door' ? '...' : 'go'}
					</button>
				</td>
				<td>
					<button
						type="button"
						on:click={() => didClickHome('door')}
						disabled={activeMotor !== null ||
							doorAction !== null ||
							movingMotor !== null ||
							isTesting === true}
					>
						home
					</button>
					<button
						type="button"
						on:click={() => didClickCrash('door')}
						disabled={activeMotor !== null ||
							doorAction !== null ||
							movingMotor !== null ||
							isTesting === true}
					>
						crash
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('open')}
						disabled={doorAction !== null ||
							activeMotor !== null ||
							movingMotor !== null ||
							isTesting === true}
					>
						open
					</button>
					<button
						type="button"
						on:click={() => didClickDoor('close')}
						disabled={doorAction !== null ||
							activeMotor !== null ||
							movingMotor !== null ||
							isTesting === true}
					>
						close
					</button>
				</td>
			</tr>
		</tbody>

		<thead>
			<tr>
				<th colspan="4">tool motors</th>
			</tr>
			<tr>
				<th>motor</th>
				<th>status</th>
				<th>move to</th>
				<th>actions</th>
			</tr>
		</thead>
		<tbody>
			{#each motors.tool as motor}
				<tr>
					<td>{motor}</td>
					<td>
						{#if testingMotor === motor}
							<span class="status-testing">TEST</span>
						{:else if testResults[motor] === 'ok'}
							<span class="status-ok">OK</span>
						{:else if testResults[motor] === 'err'}
							<span class="status-error">ERR</span>
						{:else}
							<span class="status-unknown">-</span>
						{/if}
					</td>
					<td>
						<input
							type="number"
							step="0.1"
							placeholder={unitForMotor(motor)}
							value={moveTargets[motor] ?? ''}
							on:input={(e) => didInputTarget(motor, e)}
							disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
						/>
						<button
							type="button"
							on:click={() => didClickMove(motor)}
							disabled={movingMotor !== null || activeMotor !== null || isTesting === true}
						>
							{movingMotor === motor ? '...' : 'go'}
						</button>
					</td>
					<td>
						<button
							type="button"
							on:click={() => didClickHome(motor)}
							disabled={activeMotor !== null ||
								gripperAction !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							home
						</button>
						<button
							type="button"
							on:click={() => didClickCrash(motor)}
							disabled={activeMotor !== null ||
								gripperAction !== null ||
								movingMotor !== null ||
								isTesting === true}
						>
							crash
						</button>
					</td>
				</tr>
			{/each}
			<tr>
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
				<td>
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
		margin-bottom: 1rem;
	}

	button {
		margin-top: 0;
	}

	input[type='number'] {
		width: 80px;
		margin-right: 4px;
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
