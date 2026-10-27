<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		abortCycleTest,
		continueCycleTest,
		getCycleTest,
		startCycleTest,
		type CycleTestConfig,
		type CycleTestMode,
		type CycleTestSnapshot
	} from '$lib/api';
	import { createReconnectingEventSource, type ReconnectingEventSource } from '$lib/sse';
	import { postToast } from '$lib/toasts';

	const CONFIG_STORAGE_KEY = 'opshelf.calibration.cycle-test.config';
	const MODES: { value: CycleTestMode; label: string }[] = [
		{ value: 'retrieve_store', label: 'retrieve → store' },
		{ value: 'store_retrieve', label: 'store → retrieve' },
		{ value: 'store_only', label: 'store only' },
		{ value: 'retrieve_only', label: 'retrieve only' },
		{ value: 'quick', label: 'quick pick/place' },
		{ value: 'quick_lift', label: 'quick lift' }
	];

	let mode = $state<CycleTestMode>('retrieve_store');
	let liftMm = $state<number | null>(10);
	let target = $state<string>('');
	let start = $state<string>('');
	let skip = $state<string>('');
	let shuffle = $state<boolean>(false);
	let clear = $state<boolean>(false);
	let step = $state<boolean>(false);
	let loop = $state<'once' | 'forever' | 'count'>('once');
	let loopCount = $state<number | null>(2);

	let snapshot = $state<CycleTestSnapshot | null>(null);
	let stream: ReconnectingEventSource | null = null;
	let isRequesting = $state<boolean>(false);

	const isActive = $derived(snapshot?.state === 'running' || snapshot?.state === 'paused');
	const passedCount = $derived(snapshot?.results.filter((result) => result.passed).length ?? 0);
	const doneCount = $derived(
		snapshot?.results.filter((result) => result.cell !== snapshot?.current).length ?? 0
	);
	const failedResult = $derived(snapshot?.results.find((result) => result.passed === false && result.cell !== snapshot?.current) ?? null);

	onMount(function () {
		if (browser === false) {
			return;
		}

		restoreConfig();
		void didLoad();

		return closeStream;
	});

	async function didLoad(): Promise<void> {
		try {
			applySnapshot(await getCycleTest());
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : 'failed to load cycle test state');
		}
	}

	function applySnapshot(next: CycleTestSnapshot): void {
		snapshot = next;

		if (next.state === 'running' || next.state === 'paused') {
			openStream();
		} else {
			closeStream();
		}
	}

	// Indexes on these devices are single digits, so "b32" becomes "B:3:2" as it is typed.
	function formatCellInput(value: string): string {
		return value
			.toUpperCase()
			.replace(/[^FB0-9,:]/g, '')
			.split(',')
			.map((part) => part.replace(/:/g, '').split('').join(':'))
			.join(',');
	}

	function didInputCell(event: Event, assign: (value: string) => void): void {
		const input = event.target as HTMLInputElement;
		const formatted = formatCellInput(input.value);

		input.value = formatted;
		assign(formatted);
	}

	function openStream(): void {
		if (stream !== null) {
			return;
		}

		stream = createReconnectingEventSource({
			path: '/calibration/cycle-test/stream',
			events: {
				snapshot: (event) => applySnapshot(JSON.parse((event as MessageEvent<string>).data))
			}
		});
		stream.connect();
	}

	function closeStream(): void {
		stream?.close();
		stream = null;
	}

	function buildConfig(): CycleTestConfig {
		return {
			mode,
			lift_mm: mode === 'quick_lift' ? Number(liftMm) : null,
			target: target.trim() || null,
			start: start.trim() || null,
			skip: skip.trim() || null,
			shuffle,
			clear,
			loop: loop === 'once' ? null : loop === 'forever' ? 0 : Math.max(1, Number(loopCount) || 1),
			step
		};
	}

	function persistConfig(): void {
		try {
			window.localStorage.setItem(
				CONFIG_STORAGE_KEY,
				JSON.stringify({ mode, liftMm, target, start, skip, shuffle, step, loop, loopCount })
			);
		} catch {
			return;
		}
	}

	function restoreConfig(): void {
		try {
			const stored = JSON.parse(window.localStorage.getItem(CONFIG_STORAGE_KEY) ?? 'null');

			if (stored === null || typeof stored !== 'object') {
				return;
			}

			mode = MODES.some((item) => item.value === stored.mode) ? stored.mode : mode;
			liftMm = typeof stored.liftMm === 'number' ? stored.liftMm : liftMm;
			target = typeof stored.target === 'string' ? stored.target : target;
			start = typeof stored.start === 'string' ? stored.start : start;
			skip = typeof stored.skip === 'string' ? stored.skip : skip;
			shuffle = stored.shuffle === true;
			step = stored.step === true;
			loop = ['once', 'forever', 'count'].includes(stored.loop) ? stored.loop : loop;
			loopCount = typeof stored.loopCount === 'number' ? stored.loopCount : loopCount;
		} catch {
			return;
		}
	}

	async function request(action: () => Promise<CycleTestSnapshot>, fallback: string): Promise<void> {
		isRequesting = true;

		try {
			applySnapshot(await action());
		} catch (err) {
			postToast('error', err instanceof Error ? err.message : fallback);
		} finally {
			isRequesting = false;
		}
	}

	// Server-side 422s are scrubbed to a generic sentence, so name the bad field here.
	const FIELD_PATTERNS: [string, () => string, RegExp][] = [
		['target', () => target, /^([FB]|\d+|[FB]:\d+|\d+:\d+|[FB]:\d+:\d+)$/i],
		['start', () => start, /^[FB]:\d+:\d+$/i],
		['skip', () => skip, /^[FB]:\d+(:\d+)?(\s*,\s*[FB]:\d+(:\d+)?)*$/i]
	];

	function invalidField(): string | null {
		for (const [name, read, pattern] of FIELD_PATTERNS) {
			const value = read().trim();

			if (value !== '' && pattern.test(value) === false) {
				return name;
			}
		}

		return null;
	}

	async function didClickStart(): Promise<void> {
		const field = invalidField();

		if (field !== null) {
			postToast('error', `${field} has an invalid format; see the placeholder.`);
			return;
		}

		// clear is never persisted: it deletes every item on the device, so it must be ticked per run.
		if (clear === true && confirm('delete every item and item location on this device before the test?') === false) {
			return;
		}

		persistConfig();
		await request(() => startCycleTest(buildConfig()), 'failed to start cycle test');
		clear = false;
	}

	function didClickContinue(): Promise<void> {
		return request(continueCycleTest, 'failed to continue cycle test');
	}

	function didClickAbort(): Promise<void> {
		return request(abortCycleTest, 'failed to abort cycle test');
	}

</script>

<div class="scroll-x cycle-test">
	<table>
		<thead>
			<tr>
				<th colspan="3">cycle test</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>
					<span class="field-label">mode</span>
					<select bind:value={mode} disabled={isActive}>
						{#each MODES as item}
							<option value={item.value}>{item.label}</option>
						{/each}
					</select>
				</td>
				<td>
					<span class="field-label">lift mm</span>
					<input
						type="number"
						min="1"
						max="30"
						step="0.5"
						bind:value={liftMm}
						disabled={isActive || mode !== 'quick_lift'}
					/>
				</td>
				<td>
					<span class="field-label">loop</span>
					<div class="inline">
						<select bind:value={loop} disabled={isActive}>
							<option value="once">once</option>
							<option value="count">times</option>
							<option value="forever">until abort</option>
						</select>
						{#if loop === 'count'}
							<input type="number" min="1" class="short" bind:value={loopCount} disabled={isActive} />
						{/if}
					</div>
				</td>
			</tr>
			<tr>
				<td>
					<span class="field-label">target</span>
					<input
						type="text"
						placeholder="F, 2, F:2, 2:3, F:2:3"
						value={target}
						oninput={(event) => didInputCell(event, (value) => (target = value))}
						disabled={isActive}
					/>
				</td>
				<td>
					<span class="field-label">start</span>
					<input
						type="text"
						class="cell-input"
						placeholder="B:0:3"
						value={start}
						oninput={(event) => didInputCell(event, (value) => (start = value))}
						disabled={isActive}
					/>
				</td>
				<td>
					<span class="field-label">skip</span>
					<input
						type="text"
						class="cell-input"
						placeholder="F:1"
						value={skip}
						oninput={(event) => didInputCell(event, (value) => (skip = value))}
						disabled={isActive}
					/>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<label class="toggle"><input type="checkbox" bind:checked={shuffle} disabled={isActive} /><span>shuffle</span></label>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<label class="toggle"><input type="checkbox" bind:checked={step} disabled={isActive} /><span>pause after each cell</span></label>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<label class="toggle"><input type="checkbox" bind:checked={clear} disabled={isActive} /><span>clear all items first</span></label>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="inline">
						<button type="button" onclick={didClickStart} disabled={isActive || isRequesting}>start</button>
						<button
							type="button"
							onclick={didClickContinue}
							disabled={snapshot?.state !== 'paused' || isRequesting}
						>
							continue
						</button>
						<button type="button" onclick={didClickAbort} disabled={isActive === false || isRequesting}>
							abort
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>
</div>

{#if snapshot !== null && snapshot.run_id !== null}
	<div class="scroll-x cycle-test">
		<table>
			<thead>
				<tr>
					<th colspan="2">test progress</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>state</td>
					<td>{snapshot.state}{snapshot.current !== null ? ` ${snapshot.current}` : ''}</td>
				</tr>
				{#if snapshot.config?.loop !== null && snapshot.config?.loop !== undefined}
					<tr>
						<td>loop</td>
						<td>{snapshot.loop}{snapshot.config.loop > 0 ? ` of ${snapshot.config.loop}` : ''}</td>
					</tr>
				{/if}
				<tr>
					<td>passed</td>
					<td>{passedCount}</td>
				</tr>
				{#if snapshot.total > 0}
					<tr>
						<td>remaining</td>
						<td>{snapshot.total - doneCount}</td>
					</tr>
				{/if}
				{#if failedResult !== null}
					<tr>
						<td>failed</td>
						<td class="fail">{failedResult.cell}{failedResult.error !== null ? `: ${failedResult.error}` : ''}</td>
					</tr>
				{/if}
				{#if snapshot.hardware_fault !== null}
					<tr>
						<td>hardware fault</td>
						<td class="fail">{snapshot.hardware_fault}</td>
					</tr>
				{:else if snapshot.error !== null}
					<tr>
						<td>error</td>
						<td class="fail">{snapshot.error}</td>
					</tr>
				{/if}
			</tbody>
		</table>
	</div>
{/if}

<style>
	.cycle-test {
		margin-top: 1.5em;
	}

	.field-label {
		display: block;
		font-weight: 600;
		margin-bottom: 0.25rem;
	}

	.inline {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
	}

	.toggle {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.short {
		width: 5em;
	}

	.cell-input {
		width: 6ch;
	}

	.fail {
		color: #c62828;
	}
</style>
