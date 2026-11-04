<script lang="ts">
	import { createToastSource } from '$lib/toasts';

	type Props = {
		message: string;
	};

	let { message }: Props = $props();

	// This component states what it currently observes, it does not announce it: the
	// queue decides visibility, so remounting on navigation does not re-toast a fault
	// the technician already dismissed.
	const source = createToastSource('error');

	$effect(() => {
		source.report(message);
	});

	$effect(() => () => source.release());
</script>
