<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { postToast } from '$lib/toasts';
	import {
		UNSET,
		boundsHint,
		buildRows,
		changedLeaves,
		filterRows,
		formatValue,
		getAt,
		isRegisterField,
		localIssue,
		pathId,
		setAt,
		type JsonValue,
		type PathKey,
		type Row,
		type Schema
	} from '$lib/config-editor';

	type SectionResponse = { section: string; file: string; revision: string; data: JsonValue | null };
	type Issue = { path: string; message: string };
	type ValidateResponse = { valid: boolean; issues: Issue[] };

	const SECTIONS = [
		'robot',
		'robot_app',
		'storage',
		'sizes',
		'printer',
		'scanner',
		'scale',
		'tool_camera',
		'overhead_camera',
		'overview_one_camera',
		'overview_two_camera',
		'demo'
	];
	const VALIDATE_DELAY_MS = 400;
	const RESTART_TIMEOUT_MS = 90_000;

	let section = $state('robot');
	let file = $state('');
	let revision = $state('');
	let original = $state<JsonValue>({});
	let draft = $state<JsonValue>({});
	let schema = $state<Schema>({});
	let defs = $state<Record<string, Schema>>({});
	let query = $state('');
	let expanded = $state<Set<string>>(new Set());
	let serverIssues = $state<Issue[]>([]);
	let isLoading = $state(true);
	let isValidating = $state(false);
	let isSaving = $state(false);
	let validateSeq = 0;

	const rows = $derived(buildRows(draft, schema, defs));
	const rowIds = $derived(new Set(rows.map((row) => row.id)));
	const matches = $derived(query.trim() === '' ? null : filterRows(rows, query));
	const changes = $derived(changedLeaves(original, draft));
	const changedIds = $derived(new Set(changes.map((change) => pathId(change.path))));

	const issuesByRow = $derived.by(() => {
		const byRow = new Map<string, string[]>();
		const prefix = `${section}.`;
		for (const issue of serverIssues) {
			let id = issue.path.startsWith(prefix) ? issue.path.slice(prefix.length) : issue.path === section ? '' : null;
			while (id !== null && id !== '' && rowIds.has(id) === false) {
				id = id.includes('.') ? id.slice(0, id.lastIndexOf('.')) : '';
			}
			const key = id === null || id === '' ? '' : id;
			byRow.set(key, [...(byRow.get(key) ?? []), key === '' ? `${issue.path}: ${issue.message}` : issue.message]);
		}
		for (const row of rows) {
			const local = row.field.kind === 'group' ? null : localIssue(row.field, row.value);
			if (local !== null && byRow.has(row.id) === false) {
				byRow.set(row.id, [...(byRow.get(row.id) ?? []), local]);
			}
		}
		return byRow;
	});
	const issueCount = $derived([...issuesByRow.values()].reduce((sum, list) => sum + list.length, 0));

	// rows under a group that holds an issue or a change stay findable when collapsed
	const flaggedGroups = $derived.by(() => {
		const flagged = new Set<string>();
		for (const id of [...changedIds, ...issuesByRow.keys()]) {
			const parts = id.split('.');
			for (let depth = 1; depth < parts.length; depth += 1) {
				flagged.add(parts.slice(0, depth).join('.'));
			}
		}
		return flagged;
	});

	const shownRows = $derived(
		rows.filter((row) => {
			if (matches !== null) {
				return matches.has(row.id);
			}
			for (let depth = 1; depth < row.path.length; depth += 1) {
				if (expanded.has(pathId(row.path.slice(0, depth))) === false) {
					return false;
				}
			}
			return true;
		})
	);

	async function load(): Promise<void> {
		isLoading = true;
		try {
			const [current, sectionSchema] = await Promise.all([
				apiFetch<SectionResponse>(`/system/config?section=${encodeURIComponent(section)}`),
				apiFetch<Schema>(`/system/config/schema?section=${encodeURIComponent(section)}`)
			]);
			const { ['$defs']: sharedDefs, ...rest } = sectionSchema;
			defs = (sharedDefs ?? {}) as Record<string, Schema>;
			schema = rest;
			file = current.file;
			revision = current.revision;
			original = current.data ?? {};
			draft = clone(original);
			serverIssues = [];
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'The configuration could not be read.');
		} finally {
			isLoading = false;
		}
	}

	async function validate(): Promise<void> {
		const seq = ++validateSeq;
		if (changes.length === 0) {
			serverIssues = [];
			isValidating = false;
			return;
		}
		isValidating = true;
		try {
			const result = await apiFetch<ValidateResponse>('/system/config/validate', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ section, data: draft })
			});
			if (seq === validateSeq) {
				serverIssues = result.issues;
			}
		} catch (err) {
			if (seq === validateSeq) {
				postToast('error', err instanceof Error ? err.message : 'The configuration could not be checked.');
			}
		} finally {
			if (seq === validateSeq) {
				isValidating = false;
			}
		}
	}

	$effect(() => {
		JSON.stringify(draft);
		const timer = setTimeout(validate, VALIDATE_DELAY_MS);
		return () => clearTimeout(timer);
	});

	// JSON round trip: unwraps state proxies without recursing through the JsonValue type
	function clone(value: JsonValue): JsonValue {
		return JSON.parse(JSON.stringify(value)) as JsonValue;
	}

	function setValue(path: PathKey[], value: JsonValue | typeof UNSET): void {
		draft = setAt(clone(draft), path, value);
	}

	function revert(row: Row): void {
		const before = getAt(original, row.path);
		setValue(row.path, before === undefined ? UNSET : clone(before));
	}

	function parseNumber(text: string, row: Row): JsonValue {
		if (text.trim() === '') {
			return row.field.nullable ? null : Number.NaN;
		}
		return Number(text);
	}

	function parseRaw(text: string): JsonValue {
		try {
			return JSON.parse(text) as JsonValue;
		} catch {
			return text;
		}
	}

	function toggle(row: Row): void {
		const next = new Set(expanded);
		if (next.has(row.id)) {
			next.delete(row.id);
		} else {
			next.add(row.id);
		}
		expanded = next;
	}

	function setAllExpanded(open: boolean): void {
		expanded = open ? new Set(rows.filter((row) => row.field.kind === 'group').map((row) => row.id)) : new Set();
	}

	function shownValue(row: Row): JsonValue | undefined {
		return row.present ? row.value : row.field.defaultValue;
	}

	function hex(value: JsonValue | undefined): string {
		return typeof value === 'number' && Number.isInteger(value) ? `0x${value.toString(16).toUpperCase()}` : '';
	}

	async function waitForApi(): Promise<boolean> {
		const deadline = Date.now() + RESTART_TIMEOUT_MS;
		// give the old process time to exit before the first probe
		await new Promise((resolve) => setTimeout(resolve, 2000));
		while (Date.now() < deadline) {
			try {
				await apiFetch('/health');
				return true;
			} catch {
				await new Promise((resolve) => setTimeout(resolve, 1000));
			}
		}
		return false;
	}

	async function didClickSave(): Promise<void> {
		isSaving = true;
		try {
			await apiFetch('/system/config', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ section, data: draft, revision, restart: true })
			});
			postToast('status', 'Configuration saved. The API is restarting.');
			if ((await waitForApi()) === false) {
				postToast('error', 'The API did not come back after the restart. Check the API container log.');
				return;
			}
			await load();
			postToast('status', 'The API restarted with the new configuration.');
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'The configuration could not be saved.');
		} finally {
			isSaving = false;
		}
	}

	function didChangeSection(): void {
		query = '';
		expanded = new Set();
		void load();
	}

	onMount(() => {
		void load();
	});
</script>

<svelte:head>
	<title>Config • Openshelf</title>
</svelte:head>

<div class="editor">
	<div class="toolbar">
		<select bind:value={section} onchange={didChangeSection} disabled={isSaving}>
			{#each SECTIONS as name (name)}
				<option value={name}>{name}</option>
			{/each}
		</select>
		<input
			class="search"
			type="search"
			placeholder="search, e.g. motors.stage.lim or pins.pin"
			bind:value={query}
			spellcheck="false"
			autocomplete="off"
		/>
		<button onclick={() => setAllExpanded(true)} disabled={matches !== null}>expand</button>
		<button onclick={() => setAllExpanded(false)} disabled={matches !== null}>collapse</button>
		<span class="file">{file}</span>
	</div>

	<div class="tree">
		{#if isLoading}
			<p class="muted">Loading…</p>
		{:else if shownRows.length === 0}
			<p class="muted">No field matches “{query}”.</p>
		{:else}
			<table>
				<tbody>
					{#each shownRows as row (row.id)}
						{@const issues = issuesByRow.get(row.id) ?? []}
						{@const changed = changedIds.has(row.id)}
						{@const value = shownValue(row)}
						<tr class:changed class:invalid={issues.length > 0} class:absent={row.present === false}>
							<td class="key" style={`padding-left: ${0.5 + row.depth * 1.25}em`}>
								{#if row.field.kind === 'group'}
									<button
										class="twisty"
										onclick={() => toggle(row)}
										disabled={matches !== null}
										aria-expanded={matches !== null || expanded.has(row.id)}
									>
										{matches !== null || expanded.has(row.id) ? '−' : '+'}
									</button>
								{/if}
								<span title={row.id}>{row.label}</span>
								{#if row.field.kind === 'group' && flaggedGroups.has(row.id) && expanded.has(row.id) === false && matches === null}
									<span class="flag">●</span>
								{/if}
							</td>
							<td class="value">
								{#if row.field.kind === 'group'}
									<span class="muted">
										{row.present ? `${row.childCount} ${Array.isArray(row.value) ? 'items' : 'fields'}` : 'not set, defaults apply'}
									</span>
								{:else if row.field.kind === 'boolean'}
									<input
										type="checkbox"
										checked={value === true}
										onchange={(event) => setValue(row.path, event.currentTarget.checked)}
									/>
								{:else if row.field.kind === 'enum'}
									<select
										value={JSON.stringify(value ?? null)}
										onchange={(event) => setValue(row.path, JSON.parse(event.currentTarget.value) as JsonValue)}
									>
										{#if row.field.nullable}
											<option value="null">null</option>
										{/if}
										{#each row.field.options as option (JSON.stringify(option))}
											<option value={JSON.stringify(option)}>
												{isRegisterField(row.key) ? `${hex(option)} (${option})` : String(option)}
											</option>
										{/each}
									</select>
								{:else if row.field.kind === 'integer' || row.field.kind === 'number'}
									<input
										type="number"
										step={row.field.kind === 'integer' ? 1 : 'any'}
										min={row.field.min}
										max={row.field.max}
										value={typeof value === 'number' && Number.isFinite(value) ? value : ''}
										placeholder={value === null ? 'null' : ''}
										oninput={(event) => setValue(row.path, parseNumber(event.currentTarget.value, row))}
									/>
									{#if isRegisterField(row.key)}<span class="muted">{hex(value)}</span>{/if}
									<span class="muted">{boundsHint(row.field)}</span>
								{:else if row.field.kind === 'tuple-raw'}
									<input
										type="text"
										value={JSON.stringify(value ?? null)}
										onchange={(event) => setValue(row.path, parseRaw(event.currentTarget.value))}
									/>
								{:else}
									<input
										type="text"
										value={typeof value === 'string' ? value : ''}
										placeholder={value === null ? 'null' : ''}
										oninput={(event) =>
											setValue(
												row.path,
												event.currentTarget.value === '' && row.field.nullable ? null : event.currentTarget.value
											)}
									/>
								{/if}
								{#each issues as message, index (index)}
									<div class="issue">{message}</div>
								{/each}
							</td>
							<td class="actions">
								{#if changed}
									<button onclick={() => revert(row)} title={`was ${formatValue(getAt(original, row.path))}`}>revert</button>
								{:else if row.field.kind !== 'group' && row.present && row.required === false && row.field.hasDefault}
									<button onclick={() => setValue(row.path, UNSET)} title="remove from the file; the default applies">unset</button>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	<div class="footer">
		{#if (issuesByRow.get('') ?? []).length > 0}
			<ul class="issue">
				{#each issuesByRow.get('') ?? [] as message, index (index)}
					<li>{message}</li>
				{/each}
			</ul>
		{/if}
		{#if changes.length > 0}
			<details>
				<summary>{changes.length} {changes.length === 1 ? 'change' : 'changes'}</summary>
				<ul class="changes">
					{#each changes as change (pathId(change.path))}
						<li>{pathId(change.path)}: {formatValue(change.before)} → {formatValue(change.after)}</li>
					{/each}
				</ul>
			</details>
		{/if}
		<div class="buttons">
			<span class="muted">
				{#if isValidating}checking…{:else if issueCount > 0}{issueCount} {issueCount === 1 ? 'issue' : 'issues'}{:else if changes.length > 0}valid{:else}no changes{/if}
			</span>
			<span class="spacer-flexible"></span>
			<button onclick={() => (draft = clone(original))} disabled={changes.length === 0 || isSaving}>discard</button>
			<button
				class="primary"
				onclick={didClickSave}
				disabled={changes.length === 0 || issueCount > 0 || isValidating || isSaving}
			>
				save and restart
			</button>
		</div>
	</div>
</div>

<style>
	.editor {
		display: flex;
		flex-direction: column;
		height: calc(100dvh - 9em);
		gap: 1em;
	}

	.toolbar {
		display: flex;
		gap: 0.75em;
		align-items: center;
		flex-wrap: wrap;
	}

	.search {
		flex: 1;
		min-width: 16em;
		padding: 5px;
		border: 2px solid black;
	}

	select {
		padding: 4px;
		border: 2px solid black;
		background: white;
	}

	.file {
		color: rgba(0, 0, 0, 0.5);
	}

	.tree {
		flex: 1;
		overflow: auto;
		border: 2px solid black;
	}

	.tree table {
		width: 100%;
		border: none;
	}

	.tree td {
		border: none;
		border-bottom: 1px solid rgba(0, 0, 0, 0.15);
		padding: 0.3em 0.5em;
		vertical-align: top;
	}

	.key {
		white-space: nowrap;
		width: 1%;
	}

	.value input[type='number'],
	.value input[type='text'] {
		width: 12em;
		padding: 2px 4px;
		border: 1px solid black;
	}

	.value .muted {
		margin-left: 0.5em;
	}

	.actions {
		width: 1%;
		white-space: nowrap;
	}

	.actions button,
	.twisty {
		padding: 0 4px;
		box-shadow: none;
		border-width: 1px;
	}

	.twisty {
		width: 1.6em;
		margin-right: 0.4em;
	}

	tr.changed .key {
		font-weight: bold;
		box-shadow: inset 4px 0 0 black;
	}

	tr.invalid .key {
		box-shadow: inset 4px 0 0 #c00;
	}

	tr.absent .key,
	tr.absent input {
		color: rgba(0, 0, 0, 0.45);
	}

	.issue {
		color: #c00;
		list-style: none;
	}

	.flag {
		margin-left: 0.4em;
	}

	.muted {
		color: rgba(0, 0, 0, 0.5);
	}

	.footer {
		display: flex;
		flex-direction: column;
		gap: 0.5em;
	}

	.changes {
		list-style: none;
		max-height: 8em;
		overflow: auto;
		padding: 0.5em 0;
	}

	.buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	button.primary {
		background: black;
		color: white;
	}

	button.primary:disabled {
		background: white;
		color: rgba(0, 0, 0, 0.3);
	}
</style>
