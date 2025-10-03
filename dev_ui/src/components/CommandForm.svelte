<script lang="ts">
	import { sendRobotCommand } from '../lib/api';
	import { addRecentCommand } from '../lib/storage';
	import type { CommandSchema } from '../lib/schema-parser';
	import { createDefaultValue } from '../lib/schema-parser';

	export let commands: CommandSchema[] = [];

	let selectedCommand: string = '';
	let formData: Record<string, any> = {};
	let sending: boolean = false;
	let error: string = '';

	$: selectedSchema = commands.find((c) => c.name === selectedCommand);

	function didSelectCommand(event: Event) {
		const target = event.target as HTMLSelectElement;
		selectedCommand = target.value;
		formData = {};

		if (selectedSchema !== undefined) {
			for (const [key, property] of Object.entries(selectedSchema.properties)) {
				formData[key] = createDefaultValue(property);
			}
		}
	}

	function didUpdateField(key: string, value: any) {
		formData[key] = value;
	}

	function didUpdateNestedField(parentKey: string, childKey: string, value: any) {
		if (formData[parentKey] === undefined) {
			formData[parentKey] = {};
		}

		formData[parentKey][childKey] = value;
		formData = formData;
	}

	async function didSubmitForm() {
		if (selectedCommand === '') {
			return;
		}

		sending = true;
		error = '';

		try {
			await sendRobotCommand(formData);
			addRecentCommand(formData);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Unknown error';
		} finally {
			sending = false;
		}
	}

	function renderField(key: string, property: any): boolean {
		if (property.const !== undefined) {
			return false;
		}

		return true;
	}

	export function loadCommand(command: any) {
		const commandName = command.command;

		if (commandName === undefined) {
			return;
		}

		selectedCommand = commandName;
		formData = { ...command };
	}
</script>

<div class="command-form">
	<h2>send command</h2>

	<label>
		<select value={selectedCommand} on:change={didSelectCommand}>
			{#each commands as cmd}
				<option value={cmd.name}>{cmd.name}</option>
			{/each}
		</select>
	</label>

	{#if selectedSchema !== undefined}
		<div class="form-fields">
			{#each Object.entries(selectedSchema.properties) as [key, property]}
				{#if renderField(key, property) === true}
					{#if property.type === 'object' && property.properties !== undefined}
						<fieldset>
							<legend>{key}</legend>
							{#each Object.entries(property.properties) as [childKey, childProperty]}
								<label>
									{childKey}:
									{#if childProperty.type === 'string'}
										<input
											type="text"
											value={formData[key]?.[childKey] || ''}
											on:input={(e) => didUpdateNestedField(key, childKey, e.currentTarget.value)}
										/>
									{:else if childProperty.type === 'number'}
										<input
											type="number"
											value={formData[key]?.[childKey] || 0}
											on:input={(e) => didUpdateNestedField(key, childKey, Number(e.currentTarget.value))}
										/>
									{:else if childProperty.type === 'boolean'}
										<input
											type="checkbox"
											checked={formData[key]?.[childKey] || false}
											on:change={(e) => didUpdateNestedField(key, childKey, e.currentTarget.checked)}
										/>
									{/if}
								</label>
							{/each}
						</fieldset>
					{:else}
						<label>
							{key}:
							{#if property.type === 'string'}
								<input
									type="text"
									value={formData[key] || ''}
									on:input={(e) => didUpdateField(key, e.currentTarget.value)}
								/>
							{:else if property.type === 'number'}
								<input
									type="number"
									value={formData[key] || 0}
									on:input={(e) => didUpdateField(key, Number(e.currentTarget.value))}
								/>
							{:else if property.type === 'boolean'}
								<input
									type="checkbox"
									checked={formData[key] || false}
									on:change={(e) => didUpdateField(key, e.currentTarget.checked)}
								/>
							{:else if property.type === 'object'}
								<textarea
									value={JSON.stringify(formData[key] || {}, null, 2)}
									on:input={(e) => {
										try {
											didUpdateField(key, JSON.parse(e.currentTarget.value));
										} catch {
											// ignore invalid JSON
										}
									}}
								></textarea>
							{/if}
						</label>
					{/if}
				{/if}
			{/each}
		</div>

		<button on:click={didSubmitForm} disabled={sending === true}>
			{sending === true ? 'sending...' : 'send'}
		</button>
	{/if}

	{#if error !== ''}
		<div class="error">{error}</div>
	{/if}
</div>

<style>
	.command-form {
		padding: 0;
	}

	h2 {
		margin: 0 0 1rem 0;
	}

	label {
		display: block;
		margin-bottom: 0.5rem;
	}

	select,
	input,
	textarea {
		display: block;
		width: 100%;
		margin-top: 0.25rem;
		padding: 0.25rem;
	}

	textarea {
		min-height: 100px;
	}

	.form-fields {
		margin-bottom: 1rem;
	}

	fieldset {
		padding: 1rem;
		margin: 0 0 0.5rem 0;
		border: #000000 1px solid;
	}

	legend {
		padding: 0;
		margin-bottom: 0.25rem;
	}

	button {
		cursor: pointer;
	}

	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.error {
		margin-top: 1rem;
		padding: 0.5rem;
	}
</style>
