<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { buildApiUrl, getApiOrigin } from '$lib';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';

	type SchemaObject = {
		title?: string;
		type?: string;
		format?: string;
		const?: string;
		properties?: Record<string, SchemaObject>;
		required?: string[];
		$ref?: string;
		description?: string;
	};

	type FormField = {
		path: string;
		label: string;
		type: string;
		required: boolean;
		constValue?: string;
		description?: string;
	};

	type CommandDefinition = {
		name: string;
		title: string;
		fields: FormField[];
	};

	let commandDefinitions: CommandDefinition[] = [];
	let loadError = '';
	let loading = true;

	let selectedCommand = '';
	let formValues: Record<string, string> = {};
	let submitting = false;
	let submitMessage = '';
	let submitError = '';

	onMount(() => {
		if (!browser) {
			return;
		}
		loadCommands();
	});

	async function loadCommands(): Promise<void> {
		loading = true;
		loadError = '';
		try {
			const response = await fetch(`${getApiOrigin()}/openapi.json`);
			if (!response.ok) {
				loadError = `failed to load openapi document: ${response.status}`;
				return;
			}
			const spec = (await response.json()) as Record<string, unknown>;
			const defs = parseCommandDefinitions(spec);
			if (defs.length === 0) {
				loadError = 'no robot command definitions found in openapi document';
				return;
			}
			commandDefinitions = defs;
			selectedCommand = defs[0].name;
			initialiseForm(defs[0]);
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'failed to load openapi document';
		} finally {
			loading = false;
		}
	}

	function parseCommandDefinitions(spec: Record<string, unknown>): CommandDefinition[] {
		const paths = spec.paths as Record<string, any> | undefined;
		const components = spec.components as Record<string, any> | undefined;
		const schemas = components?.schemas as Record<string, SchemaObject> | undefined;
		if (!paths || !schemas) {
			return [];
		}
		const robotPath = paths['/api/robot/commands'];
		const postOperation = robotPath?.post;
		const requestBody = postOperation?.requestBody?.content?.['application/json'];
		const rootSchema = requestBody?.schema as { oneOf?: Array<Record<string, string>> } | undefined;
		if (!rootSchema || !Array.isArray(rootSchema.oneOf)) {
			return [];
		}

		return rootSchema.oneOf
			.map((entry) => resolveSchema(entry, schemas))
			.filter((schema): schema is SchemaObject & { title: string } => Boolean(schema && schema.title))
			.map((schema) => ({
				name: schema.title as string,
				title: schema.title as string,
				fields: extractFields(schema, schemas)
			}));
	}

	function resolveSchema(entry: Record<string, string>, schemas: Record<string, SchemaObject>): SchemaObject | undefined {
		if (!entry || typeof entry.$ref !== 'string') {
			return undefined;
		}
		const parts = entry.$ref.split('/');
		const schemaName = parts[parts.length - 1];
		return schemas?.[schemaName];
	}

	function extractFields(schema: SchemaObject, schemas: Record<string, SchemaObject>, prefix = ''): FormField[] {
		const result: FormField[] = [];
		const required = new Set(schema.required ?? []);
		for (const [key, value] of Object.entries(schema.properties ?? {})) {
			const path = prefix ? `${prefix}.${key}` : key;
			const resolved = value.$ref ? resolveSchema({ $ref: value.$ref }, schemas) : value;
			if (!resolved) {
				continue;
			}
			if (resolved.type === 'object' && resolved.properties) {
				result.push(...extractFields(resolved, schemas, path));
				continue;
			}
			result.push({
				path,
				label: resolved.title ?? path,
				type: resolved.type ?? 'string',
				required: required.has(key),
				constValue: resolved.const,
				description: resolved.description
			});
		}
		return result;
	}

	function initialiseForm(definition: CommandDefinition): void {
		const next: Record<string, string> = {};
		for (const field of definition.fields) {
			if (field.constValue !== undefined) {
				next[field.path] = field.constValue;
			} else {
				next[field.path] = '';
			}
		}
		formValues = next;
		submitMessage = '';
		submitError = '';
	}

	function onCommandChange(name: string): void {
		selectedCommand = name;
		const definition = commandDefinitions.find((entry) => entry.name === name);
		if (definition) {
			initialiseForm(definition);
		}
	}

	function updateFormValue(path: string, value: string): void {
		formValues = { ...formValues, [path]: value };
	}

	async function submitCommand(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		submitMessage = '';
		submitError = '';
		const definition = commandDefinitions.find((entry) => entry.name === selectedCommand);
		if (!definition) {
			submitError = 'no command selected';
			return;
		}

		let payload: Record<string, unknown>;
		try {
			payload = buildPayload(definition);
		} catch (error) {
			submitError = error instanceof Error ? error.message : 'failed to build command payload';
			return;
		}

		submitting = true;
		try {
			const response = await fetch(buildApiUrl('/robot/commands'), {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify(payload)
			});
			if (!response.ok) {
				const text = await response.text();
				throw new Error(text || `request failed with status ${response.status}`);
			}
			submitMessage = 'command sent successfully';
		} catch (error) {
			submitError = error instanceof Error ? error.message : 'failed to send command';
		} finally {
			submitting = false;
		}
	}

	function buildPayload(definition: CommandDefinition): Record<string, unknown> {
		const payload: Record<string, unknown> = {};
		for (const field of definition.fields) {
			const raw = field.constValue ?? formValues[field.path];
			if ((raw === undefined || raw === '') && !field.constValue) {
				if (field.required) {
					throw new Error(`missing required field: ${field.label}`);
				}
				continue;
			}
			const value = convertFieldValue(field, raw);
			assignByPath(payload, field.path, value);
		}
		return payload;
	}

	function convertFieldValue(field: FormField, raw: string): unknown {
		if (field.constValue !== undefined) {
			return field.constValue;
		}
		if (field.type === 'number' || field.type === 'integer') {
			const value = Number(raw);
			if (Number.isNaN(value)) {
				throw new Error(`field ${field.label} expects a number`);
			}
			return field.type === 'integer' ? Math.trunc(value) : value;
		}
		if (field.type === 'boolean') {
			return raw === 'true';
		}
		return raw;
	}

	function assignByPath(target: Record<string, unknown>, path: string, value: unknown): void {
		const parts = path.split('.');
		let current: Record<string, unknown> = target;
		for (let index = 0; index < parts.length - 1; index += 1) {
			const key = parts[index];
			if (typeof current[key] !== 'object' || current[key] === null) {
				current[key] = {};
			}
			current = current[key] as Record<string, unknown>;
		}
		current[parts[parts.length - 1]] = value;
	}

	function inputType(field: FormField): string {
		if (field.type === 'number' || field.type === 'integer') {
			return 'number';
		}
		if (field.type === 'boolean') {
			return 'text';
		}
		return 'text';
	}
</script>

{#if loading}
	<p>loading command definitions...</p>
{:else}
	{#if loadError}
		<ErrorMessage message={loadError} />
	{:else}
		<form on:submit={submitCommand}>
			<table>
				<thead>
					<tr>
						<th colspan="2">command selection</th>
					</tr>
				</thead>
				<tbody>
					<tr>
						<td>command</td>
						<td>
							<select bind:value={selectedCommand} on:change={(event) => onCommandChange((event.currentTarget as HTMLSelectElement).value)}>
								{#each commandDefinitions as definition}
									<option value={definition.name}>{definition.title}</option>
								{/each}
							</select>
						</td>
					</tr>
				</tbody>

				{#if selectedCommand}
					{#each commandDefinitions.filter((entry) => entry.name === selectedCommand) as definition}
						<tbody>
							{#each definition.fields.filter((field) => field.path !== 'command') as field}
								<tr>
									<td>{field.label}{field.required ? ' *' : ''}</td>
									<td>
										{#if field.constValue !== undefined}
											<input value={field.constValue} readonly />
										{:else}
											<input
												type={inputType(field)}
												value={formValues[field.path] ?? ''}
												on:input={(event) => updateFormValue(field.path, (event.currentTarget as HTMLInputElement).value)}
											/>
										{/if}
									</td>
								</tr>
								{#if field.description}
									<tr>
										<td colspan="2">{field.description}</td>
									</tr>
								{/if}
							{/each}
						</tbody>
					{/each}
				{/if}
			</table>

			<button type="submit" disabled={submitting}>send</button>
		</form>

		{#if submitMessage}
			<p style="margin-top: 1em">{submitMessage}</p>
		{/if}

		{#if submitError}
			<ErrorMessage message={submitError} />
		{/if}
	{/if}
{/if}
