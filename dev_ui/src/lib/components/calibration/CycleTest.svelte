<script lang="ts">
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import {
		ApiError,
		getStorageGrid,
		getCalibrationStatus,
		homeToolMotors,
		runCycleTest,
		abortCycleTest,
		type ApiErrorDetail,
		type CalibrationStatus,
		type StorageGrid
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let storageGrid: StorageGrid | null = null;
	let loading = true;
	let loadError = '';

	let selectedSide: 'left_module' | 'right_module' = 'left_module';
	let selectedShelf = 0;
	let selectedCell = 0;

	let isRunning = false;
	let isAborting = false;
	let error = '';
	let message = '';
	let needsHome = false;
	let needsHomeReason = '';
	let needsHomeAxes: string[] = [];
	let isHoming = false;

	const TOOL_AXES = new Set(['z_axis', 'stage']);

	let needsToolHome = false;
	let needsOtherHome = false;
	let homeMessage = '';
	let duplicateHomeError = false;
	let homingDisabled = false;

	$: needsToolHome = needsHome && needsHomeAxes.some((axis) => TOOL_AXES.has(axis));
	$: needsOtherHome =
		needsHome &&
		(needsHomeAxes.length === 0 || needsHomeAxes.some((axis) => !TOOL_AXES.has(axis)));
	$: homeMessage =
		needsHome ? `home required${needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}` : '';
	$: duplicateHomeError = (() => {
		if (needsHome !== true || error === '') {
			return false;
		}

		const errorLower = error.toLowerCase();
		const reasonLower = needsHomeReason.toLowerCase();

		if (needsHomeReason !== '' && errorLower.includes(reasonLower)) {
			return true;
		}

		return needsHomeReason === '' && errorLower.includes('home required');
	})();
	$: homingDisabled =
		isHoming === true || isRunning === true || isAborting === true || needsToolHome !== true;

	onMount(function () {
		if (browser === false) {
			return;
		}
		loadStorage();
		loadCalibrationStatus();
	});

	async function loadStorage(): Promise<void> {
		loading = true;
		loadError = '';

		try {
			storageGrid = await getStorageGrid();
			selectedShelf = 0;
			selectedCell = getFirstEnabledCell();
		} catch (err) {
			handleNeedsHomeError(err);
			loadError = err instanceof Error ? err.message : 'failed to load storage grid';
		} finally {
			loading = false;
		}
	}

	async function loadCalibrationStatus(): Promise<void> {
		try {
			const status = await getCalibrationStatus();
			applyNeedsHomeStatus(status);
		} catch {
			// Ignore status polling failures; calibration actions will surface errors.
		}
	}

	function applyNeedsHomeStatus(status: CalibrationStatus): void {
		needsHome = status.needs_home;
		needsHomeReason = status.needs_home_reason ?? '';
		needsHomeAxes = status.needs_home_axes ?? [];
	}

	function applyNeedsHomeDetail(detail?: ApiErrorDetail): void {
		if (detail?.needs_home !== true) {
			return;
		}
		needsHome = true;
		needsHomeReason = detail.reason ?? detail.message ?? needsHomeReason;
		needsHomeAxes = detail.needs_home_axes ?? [];
	}

	function handleNeedsHomeError(err: unknown): void {
		if (err instanceof ApiError) {
			applyNeedsHomeDetail(err.detail);
		}
	}

	async function didClickHomeToolMotors(): Promise<void> {
		isHoming = true;
		error = '';
		message = '';

		try {
			const status = await homeToolMotors();
			applyNeedsHomeStatus(status);
			message = 'tool motors homed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'failed to home tool motors';
		} finally {
			isHoming = false;
		}
	}

	function getShelves() {
		if (storageGrid === null) {
			return [];
		}

		if (selectedSide === 'left_module') {
			return storageGrid.left_module;
		}

		return storageGrid.right_module;
	}

	function getCells() {
		const shelves = getShelves();

		if (selectedShelf >= shelves.length) {
			return [];
		}

		return shelves[selectedShelf].cells;
	}

	function getFirstEnabledCell(): number {
		const cells = getCells();

		for (let i = 0; i < cells.length; i++) {
			if (cells[i].status !== 'disabled') {
				return i;
			}
		}

		return 0;
	}

	function didChangeSide(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedSide = target.value as 'left_module' | 'right_module';
		selectedShelf = 0;
		selectedCell = getFirstEnabledCell();
		error = '';
		message = '';
	}

	function didChangeShelf(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedShelf = parseInt(target.value, 10);
		selectedCell = getFirstEnabledCell();
		error = '';
		message = '';
	}

	function didChangeCell(event: Event): void {
		const target = event.target as HTMLSelectElement;

		selectedCell = parseInt(target.value, 10);
		error = '';
		message = '';
	}

	async function didClickRunCycle(): Promise<void> {
		isRunning = true;
		error = '';
		message = '';

		try {
			await runCycleTest(selectedSide, selectedShelf, selectedCell);
			message = 'cycle test completed successfully';
		} catch (err) {
			handleNeedsHomeError(err);
			if (isAborting === false) {
				error = err instanceof Error ? err.message : 'cycle test failed';
			}
		} finally {
			isRunning = false;
		}
	}

	async function didClickAbort(): Promise<void> {
		isAborting = true;
		error = '';
		message = '';

		try {
			await abortCycleTest();
			await loadCalibrationStatus();
			message = 'cycle aborted and tool homed';
		} catch (err) {
			handleNeedsHomeError(err);
			error = err instanceof Error ? err.message : 'abort failed';
		} finally {
			isAborting = false;
		}
	}
</script>

{#if loading === true}
	<p>loading storage grid...</p>
{:else if loadError !== ''}
	<ErrorMessage message={loadError} />
{:else if storageGrid !== null}
	<table>
		<thead>
			<tr>
				<th colspan="3">cell selection</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>
					<span class="field-label">side</span>
					<select value={selectedSide} on:change={didChangeSide}>
						<option value="left_module">left module</option>
						<option value="right_module">right module</option>
					</select>
				</td>
				<td>
					<span class="field-label">shelf</span>
					<select value={selectedShelf} on:change={didChangeShelf}>
						{#each getShelves() as _, index}
							<option value={index}>{index}</option>
						{/each}
					</select>
				</td>
				<td>
					<span class="field-label">cell</span>
					<select value={selectedCell} on:change={didChangeCell}>
						{#each getCells() as cell, index}
							<option value={index} disabled={cell.status === 'disabled'}>
								{index}{cell.status === 'disabled' ? ' (disabled)' : ''}
							</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td colspan="3">
					<div class="action-buttons">
						<button
							class="run-cycle-button"
							type="button"
							on:click={didClickRunCycle}
							disabled={isRunning === true || needsHome === true || isHoming === true}
						>
							{isRunning === true ? 'running...' : 'run cycle'}
						</button>
						<button
							class="abort-button"
							type="button"
							on:click={didClickAbort}
							disabled={isRunning === false || isAborting === true}
						>
							{isAborting === true ? 'aborting...' : 'abort'}
						</button>
					</div>
				</td>
			</tr>
		</tbody>
	</table>

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if error !== ''}
		{#if duplicateHomeError === false}
			<ErrorMessage message={error} />
		{/if}
	{/if}

	<div class="section-divider"></div>

	<div class="action-buttons home-actions">
		<button type="button" on:click={didClickHomeToolMotors} disabled={homingDisabled}>
			{isHoming === true ? 'homing...' : 'home tool motors'}
		</button>
	</div>

	{#if homeMessage !== ''}
		<ErrorMessage message={homeMessage} />
	{/if}

	{#if needsOtherHome === true}
		<StatusMessage message="other motors require homing; use the motors page" />
	{/if}
{/if}

<style>
	.field-label {
		margin-right: 0.5em;
	}

	.action-buttons {
		display: flex;
		gap: 1em;
	}

	.run-cycle-button {
		margin-top: 0;
	}

	.abort-button {
		margin-top: 0;
	}

	.section-divider {
		margin-top: 2em;
		border-top: 2px black dashed;
	}

	.home-actions {
		margin-top: 2em;
		align-items: center;
	}

	.home-actions button {
		margin-top: 0;
	}

</style>
