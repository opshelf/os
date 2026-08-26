<script lang="ts">
	import type { RobotInitComponent, RobotInitState } from '$lib';

	type Props = {
		init: RobotInitState | null;
	};

	let { init }: Props = $props();

	const labels: Record<string, string> = {
		config: 'configuration',
		motor_bus: 'motor bus',
		scale_bus: 'scale bus',
		laser_bus: 'laser bus',
		gripper: 'gripper',
		x_axis: 'x axis',
		y_axis: 'y axis',
		z_axis: 'z axis',
		stage: 'stage',
		rotate: 'rotate',
		laser: 'laser',
		door: 'door',
		scale: 'scale',
		light_curtain: 'light curtain',
		cabinet: 'cabinet',
		command_worker: 'command worker',
		barcode: 'barcode scanner',
		printer: 'printer',
		tool_camera: 'tool camera',
		overhead_camera: 'overhead camera',
		overview_one_camera: 'overview camera one',
		overview_two_camera: 'overview camera two',
		iobox_sizer_camera: 'sizer camera'
	};

	function label(component: string): string {
		return labels[component] ?? component.replace(/_/g, ' ');
	}

	type StepState = 'done' | 'current' | 'pending' | 'failed';

	function stepState(phase: string): StepState {
		if (phase === 'ready') {
			return 'done';
		}
		if (phase === 'failed') {
			return 'failed';
		}
		if (phase === 'pending') {
			return 'pending';
		}
		return 'current';
	}

	const isInitializing = $derived(init !== null && init.phase === 'initializing');

	const visibleComponents = $derived(
		init === null
			? []
			: isInitializing
				? init.components
				: init.components.filter((component) => stepState(component.phase) !== 'done')
	);

	function statusText(component: RobotInitComponent): string {
		const state = stepState(component.phase);

		if (state === 'done') {
			return 'done';
		}

		if (state === 'pending') {
			return 'pending';
		}

		if (state === 'failed') {
			return component.reason ? `failed — ${component.reason}` : 'failed';
		}

		return component.phase;
	}
</script>

{#if init !== null}
	<table class="init-progress">
		<thead>
			<tr>
				<th colspan="3"> robot initialization </th>
			</tr>
		</thead>
		<tbody>
			{#each visibleComponents as component (component.component)}
				{@const state = stepState(component.phase)}
				<tr class="step step-{state}">
					<td class="step-marker">●</td>
					<td class="step-name">{label(component.component)}</td>
					<td class="step-status">{statusText(component)}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.init-progress {
		margin-bottom: 1.5em;
	}

	.step-marker {
		text-align: center;
		width: 2em;
	}

	.step-status {
		text-transform: uppercase;
	}

	.step-done {
		color: #00a210;
	}

	.step-current {
		color: black;
		font-weight: bold;
	}

	.step-pending {
		color: #8a8a8a;
	}

	.step-failed {
		color: #c40000;
	}
</style>
