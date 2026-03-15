<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getCalibrationStatus,
		getLaserReading,
		getStorageGrid,
		homeToolMotors,
		jogAxis,
		moveToCell,
		type CalibrationStatus,
		type Position,
		type Shelf,
		type StorageGrid
	} from '$lib/api';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	type Side = 'left_module' | 'right_module';
	type JogAxis = 'x' | 'y' | 'z' | 'stage';

	const TOOL_AXES = new Set(['z_axis', 'stage']);
	const JOG_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100];

	let storageGrid = $state<StorageGrid | null>(null);
	let selectedSide = $state<Side>('left_module');
	let selectedShelf = $state<number>(0);
	let selectedCell = $state<number>(0);
	let jogStep = $state<number>(1);
	let currentPosition = $state<Position | null>(null);
	let laserDistance = $state<number | null>(null);
	let error = $state<string>('');
	let message = $state<string>('');
	let warning = $state<string>('');
	let loading = $state<boolean>(true);
	let isMoving = $state<boolean>(false);
	let isJogging = $state<boolean>(false);
	let isReadingLaser = $state<boolean>(false);
	let isHoming = $state<boolean>(false);
	let needsHome = $state<boolean>(false);
	let needsHomeReason = $state<string>('');
	let needsHomeAxes = $state<string[]>([]);

	const needsToolHome = $derived(needsHome && needsHomeAxes.some((axis) => TOOL_AXES.has(axis)));

	onMount(function () {
		if (browser === false) {
			return;
		}

		void didLoadPage();
	});

	function getShelvesForSide(side: Side): Shelf[] {
		if (storageGrid === null) {
			return [];
		}

		if (side === 'left_module') {
			return storageGrid.left_module;
		}

		return storageGrid.right_module;
	}

	function syncSelection(nextShelfIdx: number, nextCellIdx: number): void {
		const shelves = getShelvesForSide(selectedSide);
		if (shelves.length === 0) {
			selectedShelf = 0;
			selectedCell = 0;
			return;
		}

		const shelf = shelves.find((candidate) => candidate.shelf_idx === nextShelfIdx) ?? shelves[0];
		selectedShelf = shelf.shelf_idx;

		if (shelf.cells.length === 0) {
			selectedCell = 0;
			return;
		}

		const cell = shelf.cells.find((candidate) => candidate.cell_idx === nextCellIdx) ?? shelf.cells[0];
		selectedCell = cell.cell_idx;
	}

	function applyNeedsHomeStatus(status: CalibrationStatus): void {
		needsHome = status.needs_home;
		needsHomeReason = status.needs_home_reason ?? '';
		needsHomeAxes = status.needs_home_axes ?? [];
	}

	async function didLoadPage(): Promise<void> {
		loading = true;
		error = '';

		try {
			const [grid, status] = await Promise.all([getStorageGrid(), getCalibrationStatus()]);
			storageGrid = grid;
			applyNeedsHomeStatus(status);
			syncSelection(selectedShelf, selectedCell);
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to load laser page';
		} finally {
			loading = false;
		}
	}

	async function didClickMove(): Promise<void> {
		isMoving = true;
		error = '';
		message = '';
		warning = '';

		try {
			currentPosition = await moveToCell(selectedSide, selectedShelf, selectedCell);
			message = `moved to ${selectedSide}:${selectedShelf}:${selectedCell}`;
			warning = currentPosition.warnings?.join(' ') ?? '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to move to cell';
		} finally {
			isMoving = false;
		}
	}

	async function didClickJog(axis: JogAxis, delta: number): Promise<void> {
		isJogging = true;
		error = '';
		message = '';
		warning = '';

		try {
			currentPosition = await jogAxis(axis, delta);
			message = `jogged ${axis} by ${delta.toFixed(2)}mm`;
			warning = currentPosition.warnings?.join(' ') ?? '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to jog axis';
		} finally {
			isJogging = false;
		}
	}

	async function didClickReadLaser(): Promise<void> {
		isReadingLaser = true;
		error = '';
		message = '';

		try {
			const reading = await getLaserReading();
			laserDistance = reading.distance_mm;
			message = 'laser reading updated';
		} catch (err) {
			error = err instanceof Error ? err.message : 'failed to read laser';
		} finally {
			isReadingLaser = false;
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
			error = err instanceof Error ? err.message : 'failed to home tool motors';
		} finally {
			isHoming = false;
		}
	}

	function didChangeSide(): void {
		syncSelection(selectedShelf, selectedCell);
	}

	function didChangeShelf(): void {
		syncSelection(selectedShelf, selectedCell);
	}

	function didClickJogButton(event: MouseEvent): void {
		const button = event.currentTarget;
		if (!(button instanceof HTMLButtonElement)) {
			return;
		}

		const axis = button.dataset.axis;
		const direction = button.dataset.direction;
		if (
			(axis !== 'x' && axis !== 'y' && axis !== 'z' && axis !== 'stage') ||
			(direction !== '1' && direction !== '-1')
		) {
			return;
		}

		void didClickJog(axis, jogStep * Number(direction));
	}
</script>

{#if loading === true}
	<p>loading laser tools...</p>
{:else}
	<table>
		<thead>
			<tr>
				<th colspan="2">laser tools</th>
			</tr>
		</thead>
		<tbody>
			<tr>
				<td>side</td>
				<td>
					<select bind:value={selectedSide} onchange={didChangeSide}>
						<option value="left_module">left</option>
						<option value="right_module">right</option>
					</select>
				</td>
			</tr>
			<tr>
				<td>shelf</td>
				<td>
					<select bind:value={selectedShelf} onchange={didChangeShelf}>
						{#each getShelvesForSide(selectedSide) as shelf}
							<option value={shelf.shelf_idx}>{shelf.shelf_idx}</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td>cell</td>
				<td>
					<select bind:value={selectedCell}>
						{#each getShelvesForSide(selectedSide).find((shelf) => shelf.shelf_idx === selectedShelf)?.cells ?? [] as cell}
							<option value={cell.cell_idx}>{cell.cell_idx}</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td>jog step</td>
				<td>
					<select bind:value={jogStep}>
						{#each JOG_STEPS as step}
							<option value={step}>{step}mm</option>
						{/each}
					</select>
				</td>
			</tr>
			<tr>
				<td>position</td>
				<td>
					{#if currentPosition === null}
						-
					{:else}
						x={currentPosition.x.toFixed(2)} y={currentPosition.y.toFixed(2)}
						z={currentPosition.z_offset.toFixed(2)} stage={currentPosition.slide_extension.toFixed(2)}
					{/if}
				</td>
			</tr>
			<tr>
				<td>laser</td>
				<td>{laserDistance === null ? '-' : `${laserDistance.toFixed(3)} mm`}</td>
			</tr>
		</tbody>
	</table>

	<div class="actions">
		<button type="button" onclick={didClickMove} disabled={isMoving === true || isJogging === true}>
			{isMoving === true ? 'moving...' : 'move to cell'}
		</button>
		<button
			type="button"
			onclick={didClickReadLaser}
			disabled={isReadingLaser === true || isMoving === true || isJogging === true}
		>
			{isReadingLaser === true ? 'reading...' : 'read laser'}
		</button>
		<button
			type="button"
			onclick={didClickHomeToolMotors}
			disabled={isHoming === true || needsToolHome === false}
		>
			{isHoming === true ? 'homing...' : 'home tool motors'}
		</button>
	</div>

	<div class="jog-grid">
		<button type="button" data-axis="x" data-direction="-1" onclick={didClickJogButton}>x-</button>
		<button type="button" data-axis="x" data-direction="1" onclick={didClickJogButton}>x+</button>
		<button type="button" data-axis="y" data-direction="-1" onclick={didClickJogButton}>y-</button>
		<button type="button" data-axis="y" data-direction="1" onclick={didClickJogButton}>y+</button>
		<button type="button" data-axis="z" data-direction="-1" onclick={didClickJogButton}>z-</button>
		<button type="button" data-axis="z" data-direction="1" onclick={didClickJogButton}>z+</button>
		<button type="button" data-axis="stage" data-direction="-1" onclick={didClickJogButton}>stage-</button>
		<button type="button" data-axis="stage" data-direction="1" onclick={didClickJogButton}>stage+</button>
	</div>

	{#if needsToolHome === true}
		<p class="warning">tool home required{needsHomeReason !== '' ? `: ${needsHomeReason}` : ''}</p>
	{/if}

	{#if warning !== ''}
		<StatusMessage message={warning} />
	{/if}

	{#if message !== ''}
		<StatusMessage message={message} />
	{/if}

	{#if error !== ''}
		<ErrorMessage message={error} />
	{/if}
{/if}

<style>
	table {
		margin-bottom: 1rem;
	}

	.actions {
		display: flex;
		gap: 0.75rem;
		margin-bottom: 1rem;
	}

	.jog-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(8rem, 10rem));
		gap: 0.75rem;
		margin-bottom: 1rem;
	}

	.warning {
		color: #b45309;
		font-weight: 600;
	}
</style>
