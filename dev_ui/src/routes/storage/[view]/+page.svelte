<script lang="ts">
	import { error } from '@sveltejs/kit';
	import { page } from '$app/state';
	import CalibrationPage from '$lib/components/calibration/CalibrationPage.svelte';

	type CalibrationView =
		| 'layout'
		| 'cell_auto'
		| 'cell_manual'
		| 'io'
		| 'rotation'
		| 'fridge'
		| 'test';

	const views: Record<string, CalibrationView> = {
		layout: 'layout',
		auto: 'cell_auto',
		cells: 'cell_manual',
		'io-box': 'io',
		'tool-rotation': 'rotation',
		fridge: 'fridge',
		test: 'test'
	};

	const selectedView = $derived.by(() => {
		const viewParam = page.params.view;
		const view = viewParam ? views[viewParam] : undefined;

		if (view === undefined) {
			error(404, 'calibration tab not found');
		}

		return view;
	});
</script>

<CalibrationPage {selectedView} />
