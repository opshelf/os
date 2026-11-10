<script lang="ts">
	import { onMount } from 'svelte';
	import { ApiError, apiFetch } from '$lib/api';
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
		pruneAbsent,
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
	let loadedSection = $state('robot');
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
	let loadSeq = 0;
	let editor = $state<HTMLDivElement>();
	let editorTop = $state(0);
	let changesDialog = $state<HTMLDialogElement>();

	const rows = $derived(buildRows(draft, schema, defs));
	const rowIds = $derived(new Set(rows.map((row) => row.id)));
	const matches = $derived(query.trim() === '' ? null : filterRows(rows, query));
	const changes = $derived(changedLeaves(original, draft));
	const changedIds = $derived(new Set(changes.map((change) => pathId(change.path))));

	const issuesByRow = $derived.by(() => {
		const byRow = new Map<string, string[]>();
		const prefix = `${loadedSection}.`;
		for (const issue of serverIssues) {
			let id = issue.path.startsWith(prefix) ? issue.path.slice(prefix.length) : issue.path === loadedSection ? '' : null;
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
	const groupIds = $derived(rows.filter((row) => row.field.kind === 'group').map((row) => row.id));
	const isAllExpanded = $derived(groupIds.length > 0 && groupIds.every((id) => expanded.has(id)));
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
		const seq = ++loadSeq;
		const target = section;
		isLoading = true;
		try {
			const [current, sectionSchema] = await Promise.all([
				apiFetch<SectionResponse>(`/system/config?section=${encodeURIComponent(target)}`),
				apiFetch<Schema>(`/system/config/schema?section=${encodeURIComponent(target)}`)
			]);
			if (seq !== loadSeq) {
				return;
			}
			const { ['$defs']: sharedDefs, ...rest } = sectionSchema;
			defs = (sharedDefs ?? {}) as Record<string, Schema>;
			schema = rest;
			loadedSection = target;
			revision = current.revision;
			original = current.data ?? {};
			draft = clone(original);
			serverIssues = [];
		} catch (err) {
			if (seq === loadSeq) {
				section = loadedSection;
				postToast('error', err instanceof Error ? err.message : 'The configuration could not be read.');
			}
		} finally {
			if (seq === loadSeq) {
				isLoading = false;
			}
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
				body: JSON.stringify({ section: loadedSection, data: draft })
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
		const next = setAt(clone(draft), path, value);
		draft = value === UNSET ? pruneAbsent(next, original, path) : next;
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

	// Writes the draft back only when it differs from the typed number, so in-progress text
	// ("-", "-0", ".05") is not rewritten while typing; revert/discard/reload still update the field.
	function syncNumber(node: HTMLInputElement, value: JsonValue | undefined): void {
		const text = typeof value === 'number' && Number.isFinite(value) ? String(value) : '';
		if (node.value === text || (text !== '' && node.value !== '' && Number(node.value) === value)) {
			return;
		}
		node.value = text;
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

	function toggleAll(): void {
		expanded = isAllExpanded ? new Set() : new Set(groupIds);
	}

	// the editor fills the viewport down to the page margin, wherever the nav ends
	function measure(): void {
		if (editor !== undefined) {
			editorTop = editor.getBoundingClientRect().top + window.scrollY;
		}
	}

	function didClickDialog(event: MouseEvent): void {
		if (event.target === changesDialog) {
			changesDialog?.close();
		}
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
				// /health answers 503 while robot init has failed; the config route does not depend on it
				await apiFetch(`/system/config?section=${encodeURIComponent(loadedSection)}`);
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
				body: JSON.stringify({ section: loadedSection, data: draft, revision })
			});
			postToast('status', 'Configuration saved. The API is restarting.');
			if ((await waitForApi()) === false) {
				postToast('error', 'The API did not come back after the restart. Check the API container log.');
				return;
			}
			await load();
			let isRobotReady = true;
			try {
				await apiFetch('/health');
			} catch (err) {
				isRobotReady = !(err instanceof ApiError && err.status === 503);
			}
			if (isRobotReady) {
				postToast('status', 'The API restarted with the new configuration.');
			} else {
				postToast('error', 'The API restarted, but the robot did not initialize with the new configuration.');
			}
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
		measure();
		void load();
	});
</script>

<svelte:head>
	<title>Config • Openshelf</title>
</svelte:head>

<svelte:window onresize={measure} />

<div class="editor" bind:this={editor} style={`height: calc(100dvh - ${editorTop}px - var(--page-margin))`}>
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
		<button class="toggle-all" onclick={toggleAll} disabled={matches !== null || groupIds.length === 0}>
			{isAllExpanded ? 'collapse' : 'expand'}
		</button>
	</div>

	<div class="tree">
		{#if isLoading}
			<p class="muted message">Loading…</p>
		{:else if shownRows.length === 0}
			<p class="muted message">No field matches “{query}”.</p>
		{:else}
			<table>
				<tbody>
					{#each shownRows as row (row.id)}
						{@const issues = issuesByRow.get(row.id) ?? []}
						{@const changed = changedIds.has(row.id)}
						{@const value = shownValue(row)}
						<tr class:changed class:invalid={issues.length > 0} class:absent={row.present === false}>
							<td class="key" style={`padding-left: calc(0.5em + ${row.depth} * (var(--control) + 0.5em) / 2)`}>
								{#if row.field.kind === 'group'}
									<button
										class="twisty"
										onclick={() => toggle(row)}
										disabled={matches !== null}
										aria-expanded={matches !== null || expanded.has(row.id)}
									>
										{matches !== null || expanded.has(row.id) ? '−' : '+'}
									</button>
								{:else}
									<span class="twisty-space"></span>
								{/if}
								<span class="text" title={row.id}>{row.label}</span>
								{#if row.field.kind === 'group' && flaggedGroups.has(row.id) && expanded.has(row.id) === false && matches === null}
									<span class="text flag">●</span>
								{/if}
							</td>
							{#if row.field.kind === 'group'}
								<!-- spans to the table edge so the count stays put when deeper rows widen the key column -->
								<td class="count" colspan="2">
									<span class="text muted">
										{row.present ? `${row.childCount} ${Array.isArray(row.value) ? 'items' : 'fields'}` : 'not set, defaults apply'}
									</span>
									{#each issues as message, index (index)}
										<div class="issue">{message}</div>
									{/each}
								</td>
							{:else}
							<td class="value">
								{#if row.field.kind === 'boolean'}
									<span class="text">
										<input
											type="checkbox"
											checked={value === true}
											onchange={(event) => setValue(row.path, event.currentTarget.checked)}
										/>
									</span>
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
										{@attach (node) => syncNumber(node, value)}
										placeholder={value === null ? 'null' : ''}
										oninput={(event) => setValue(row.path, parseNumber(event.currentTarget.value, row))}
									/>
									{#if isRegisterField(row.key)}<span class="text muted hint">{hex(value)}</span>{/if}
									<span class="text muted hint">{boundsHint(row.field)}</span>
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
								{:else if row.present && row.required === false && row.field.hasDefault}
									<button onclick={() => setValue(row.path, UNSET)} title="remove from the file; the default applies">unset</button>
								{/if}
							</td>
							{/if}
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
		<div class="buttons">
			<span class="status" class:invalid={isValidating === false && issueCount > 0} class:valid={isValidating === false && issueCount === 0 && changes.length > 0}>
				{#if isValidating}checking…{:else if issueCount > 0}{issueCount} invalid{:else if changes.length > 0}valid{:else}no changes{/if}
			</span>
			<button onclick={() => changesDialog?.showModal()} disabled={changes.length === 0}>
				show changes
			</button>
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

<dialog class="changes-dialog" bind:this={changesDialog} onclick={didClickDialog}>
	<div class="dialog-head">
		<span>{changes.length} {changes.length === 1 ? 'change' : 'changes'} in {loadedSection}</span>
		<span class="spacer-flexible"></span>
		<button onclick={() => changesDialog?.close()}>close</button>
	</div>
	<ul class="changes">
		{#each changes as change (pathId(change.path))}
			<li>
				<span class="change-path">{pathId(change.path)}</span>
				<span>{formatValue(change.before)} → {formatValue(change.after)}</span>
			</li>
		{:else}
			<li class="muted">no changes</li>
		{/each}
	</ul>
</dialog>

<style>
	.editor {
		/* one control height for every field, select and button on the page */
		--control: calc(1.2em + 14px);
		display: flex;
		flex-direction: column;
		gap: 1em;
		min-height: 20em;
	}

	.editor :global(button),
	.editor input:not([type='checkbox']),
	.editor select,
	.changes-dialog button {
		height: var(--control);
		line-height: 1.2;
		padding: 5px;
		border: 2px solid black;
		background-color: white;
	}

	.editor button:disabled {
		color: rgba(0, 0, 0, 0.3);
	}

	.toolbar {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.search {
		flex: 1;
		min-width: 10em;
	}

	.tree {
		flex: 1;
		overflow: auto;
		border: 2px solid black;
	}

	.message {
		padding: 0.5em;
	}

	.tree table {
		width: 100%;
		border: none;
	}

	.tree td {
		border: none;
		border-bottom: 2px solid black;
		padding: 0.5em;
		vertical-align: top;
	}

	.tree tr:last-child td {
		border-bottom: none;
	}

	/* text cells line up with the controls beside them */
	.text {
		display: inline-block;
		line-height: 1.2;
		padding: 7px 0;
		vertical-align: top;
	}

	.key {
		white-space: nowrap;
		width: 1%;
	}

	.twisty,
	.twisty-space {
		display: inline-block;
		width: var(--control);
		margin-right: 0.5em;
		vertical-align: top;
	}

	.twisty {
		padding: 5px 0;
		text-align: center;
	}

	.value input[type='number'],
	.value input[type='text'],
	.value select {
		width: 12em;
		vertical-align: top;
	}

	.value input[type='checkbox'] {
		vertical-align: middle;
	}

	.value {
		white-space: nowrap;
	}

	.hint {
		margin-left: 0.5em;
	}

	.count {
		text-align: right;
	}

	/* when deep keys make the table wider than the tree, keep the count at the visible edge */
	.count .text {
		position: sticky;
		right: 0.5em;
	}

	.actions {
		width: 1%;
		white-space: nowrap;
		text-align: right;
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
		white-space: normal;
		padding-top: 0.25em;
	}

	.flag {
		margin-left: 0.5em;
	}

	.muted {
		color: rgba(0, 0, 0, 0.5);
	}

	.footer {
		display: flex;
		flex-direction: column;
		gap: 0.5em;
	}

	.buttons {
		display: flex;
		gap: 1em;
		align-items: center;
	}

	.status {
		text-transform: uppercase;
		color: rgba(0, 0, 0, 0.5);
	}

	.status.invalid {
		color: #c00;
	}

	.status.valid {
		color: #007a00;
	}

	button.primary {
		background-color: black;
		color: white;
	}

	button.primary:disabled {
		background-color: white;
		color: rgba(0, 0, 0, 0.3);
	}

	.changes-dialog {
		margin: auto;
		width: min(48em, calc(100vw - 2 * var(--page-margin)));
		max-height: 70vh;
		padding: 0;
		border: 2px solid black;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
	}

	.changes-dialog[open] {
		display: flex;
		flex-direction: column;
	}

	.changes-dialog::backdrop {
		background-color: rgba(0, 0, 0, 0.4);
	}

	.dialog-head {
		display: flex;
		align-items: center;
		gap: 1em;
		padding: 0.5em;
		border-bottom: 2px solid black;
		text-transform: uppercase;
	}

	.changes {
		overflow: auto;
		list-style: none;
	}

	.changes li {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1em;
		/* same height as the header row, whose close button adds 7px of border and padding */
		line-height: 1.2;
		padding: calc(0.5em + 7px) 0.5em;
		border-bottom: 2px solid black;
	}

	.changes li:last-child {
		border-bottom: none;
	}

	.change-path {
		font-weight: bold;
	}
</style>
