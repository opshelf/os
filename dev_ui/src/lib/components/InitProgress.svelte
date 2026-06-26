<script lang="ts">
	import type { RobotInitState } from '$lib';

	type Props = {
		init: RobotInitState | null;
	};

	let { init }: Props = $props();

	const phaseLabels: Record<string, string> = {
		pending: 'pending',
		connecting: 'connecting',
		configuring: 'configuring',
		homing: 'homing',
		ready: 'ready',
		failed: 'failed'
	};

	function phaseLabel(phase: string): string {
		return phaseLabels[phase] ?? phase;
	}

	function overallLabel(phase: string): string {
		if (phase === 'ready') {
			return 'ROBOT READY';
		}
		if (phase === 'degraded') {
			return 'ROBOT DEGRADED';
		}
		if (phase === 'failed') {
			return 'INITIALIZATION FAILED';
		}
		return 'INITIALIZING HARDWARE';
	}
</script>

{#if init !== null}
	<div class="init-progress" class:degraded={init.phase === 'degraded'} class:failed={init.phase === 'failed'}>
		<div class="init-header">
			<span class="init-overall init-{init.phase}">{overallLabel(init.phase)}</span>
			{#if init.error}
				<span class="init-error">{init.error}</span>
			{/if}
		</div>

		{#if init.components.length > 0}
			<ul class="init-components">
				{#each init.components as component (component.component)}
					<li class="init-component init-phase-{component.phase}">
						<span class="component-name">{component.component}</span>
						<span class="component-phase">{phaseLabel(component.phase)}</span>
						{#if component.reason}
							<span class="component-reason">{component.reason}</span>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}

<style>
	.init-progress {
		border: 1px solid #2c2c2c;
		border-radius: 4px;
		padding: 0.75em 1em;
		margin: 0.5em 0;
		background: #0c0c0c;
		font-family: monospace;
	}

	.init-progress.degraded {
		border-color: #b8860b;
	}

	.init-progress.failed {
		border-color: #a21010;
	}

	.init-header {
		display: flex;
		align-items: center;
		gap: 1em;
		margin-bottom: 0.5em;
	}

	.init-overall {
		font-weight: bold;
		letter-spacing: 0.05em;
	}

	.init-ready {
		color: #00a210;
	}

	.init-initializing {
		color: #d8d8d8;
	}

	.init-degraded {
		color: #d8a51b;
	}

	.init-failed {
		color: #e25555;
	}

	.init-error {
		color: #e25555;
	}

	.init-components {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: 0.25em 1em;
	}

	.init-component {
		display: flex;
		align-items: baseline;
		gap: 0.5em;
	}

	.component-name {
		min-width: 6em;
		color: #d8d8d8;
	}

	.component-phase {
		color: #888;
	}

	.init-phase-ready .component-phase {
		color: #00a210;
	}

	.init-phase-homing .component-phase,
	.init-phase-configuring .component-phase,
	.init-phase-connecting .component-phase {
		color: #d8a51b;
	}

	.init-phase-failed .component-phase {
		color: #e25555;
	}

	.component-reason {
		color: #e25555;
		font-style: italic;
	}
</style>
