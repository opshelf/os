<script lang="ts">
	import { error } from '@sveltejs/kit';
	import { page } from '$app/state';
	import CalibrationPage from '$lib/components/calibration/CalibrationPage.svelte';

	type CalibrationView = 'cell_auto' | 'cell_manual' | 'rotation' | 'test';

	const views: Record<string, CalibrationView> = {
		auto: 'cell_auto',
		cells: 'cell_manual',
		'tool-rotation': 'rotation',
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
