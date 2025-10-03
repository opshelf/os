<script lang="ts">
	import { onMount } from 'svelte';
	import CommandForm from './components/CommandForm.svelte';
	import RecentCommands from './components/RecentCommands.svelte';
	import StatusLog from './components/StatusLog.svelte';
	import { fetchOpenApiSchema } from './lib/api';
	import { parseCommandSchemas } from './lib/schema-parser';
	import type { CommandSchema } from './lib/schema-parser';

	let commands: CommandSchema[] = [];
	let loading: boolean = true;
	let error: string = '';
	let commandFormRef: CommandForm;

	onMount(async () => {
		try {
			const schema = await fetchOpenApiSchema();
			commands = parseCommandSchemas(schema);
			loading = false;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load schema';
			loading = false;
		}
	});

	function didSelectRecentCommand(command: any) {
		if (commandFormRef !== undefined) {
			commandFormRef.loadCommand(command);
		}
	}
</script>

<div class="app">
	{#if loading === true}
		<p>loading schema...</p>
	{:else if error !== ''}
		<p>error: {error}</p>
	{:else}
		<div class="top-section">
			<div class="left">
				<CommandForm bind:this={commandFormRef} {commands} />
			</div>
			<div class="separator"></div>
			<div class="right">
				<RecentCommands onCommandSelect={didSelectRecentCommand} />
			</div>
		</div>

		<div class="horizontal-separator"></div>

		<div class="bottom-section">
			<StatusLog />
		</div>
	{/if}
</div>

<style>
	.app {
		padding: 1rem;
		height: 100vh;
		display: flex;
		flex-direction: column;
	}

	.top-section {
		display: grid;
		grid-template-columns: 1fr 1px 1fr;
		gap: 1rem;
	}

	.separator {
		background-color: black;
		width: 1px;
	}

	.horizontal-separator {
		height: 1px;
		background-color: black;
		margin: 1rem 0;
	}

	.bottom-section {
		flex: 1;
		min-height: 0;
	}
</style>
