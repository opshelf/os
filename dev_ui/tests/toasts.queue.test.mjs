// Queue semantics for the toast store: node tests/toasts.queue.test.mjs
// SLOW=1 adds the acknowledgement-window checks, which wait out a real 60s timer.
import { get } from 'svelte/store';
import { createToastSource, dismissToast, postToast, toasts } from '../src/lib/toasts.ts';

const out = [];
const check = (label, ok) => out.push(`${ok ? 'PASS' : 'FAIL'} ${label}`);
const visible = () => get(toasts).map((toast) => toast.message);
const has = (message) => visible().includes(message);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const dismiss = (message) => {
	const toast = get(toasts).find((candidate) => candidate.message === message);

	if (toast !== undefined) {
		dismissToast(toast.id);
	}
};

const s1 = createToastSource('error');
const s2 = createToastSource('error');

s1.report('motor x faulted');
check('ongoing condition shows on first report', has('motor x faulted'));

s1.report('motor x faulted');
check('re-report while visible does not stack', visible().length === 1);

dismiss('motor x faulted');
check('tap dismisses', has('motor x faulted') === false);

s1.report('motor x faulted');
s2.report('motor x faulted');
check(
	'dismissed stays dismissed while unchanged (remount + second source)',
	has('motor x faulted') === false
);

s1.report('motor x faulted (following error)');
check('changed wording re-shows without an extra tap', has('motor x faulted (following error)'));

s1.report('');
check('heal removes it from the queue', has('motor x faulted (following error)') === false);

s1.report('motor x faulted (following error)');
check('same fault after a heal is shown again', has('motor x faulted (following error)'));

s1.report('');
s2.report('');
check('last source healing clears it', visible().length === 0);

s1.report('camera 1 is offline');
s1.report('api or server restarted');
check(
	'a source changing state ends its previous condition',
	has('camera 1 is offline') === false && has('api or server restarted')
);
s1.report('');

s1.report('unmount test');
s1.release();
check('unmount (release) keeps a real fault on screen', has('unmount test'));
s1.report('');

for (let i = 1; i <= 8; i++) {
	postToast('error', `event ${i}`);
}
check(
	'queue caps visible toasts and drops the oldest',
	visible().length === 5 && visible()[0] === 'event 4' && visible()[4] === 'event 8'
);
visible().forEach((message) => dismiss(message));
check('dismissing leaves the queue empty', visible().length === 0);

s1.report('motor x faulted');
s2.report('motor x faulted');
check('two sources with the same fault share one slot', visible().length === 1);
s1.report('');
check('toast stays while another source still reports the fault', has('motor x faulted'));
s2.report('');
check('queue empties when both sources heal', visible().length === 0);

const needsHome = createToastSource('status');
needsHome.report('motors need homing', { key: 'needs-home:x:y', actions: [] });
needsHome.report('motors need homing', { key: 'needs-home:x', actions: [] });
check(
	'a new motor set re-shows under a new key',
	visible().length === 1 && visible()[0] === 'motors need homing'
);
needsHome.report('');
check('clearing the robot flag clears its toast', visible().length === 0);

if (process.env.SLOW === '1') {
	const live = createToastSource('error');
	const gone = createToastSource('error');

	live.report('x axis is faulted');
	gone.report('printer is offline');
	await wait(0);
	dismiss('x axis is faulted');
	dismiss('printer is offline');
	live.report('x axis is faulted');
	gone.report('printer is offline');
	gone.report('');
	await wait(0);
	check(
		'dismissed conditions stay hidden at first',
		has('x axis is faulted') === false && has('printer is offline') === false
	);
	console.log('waiting out the acknowledgement window...');
	await wait(61000);
	check('a fault a page still reports returns on its own', has('x axis is faulted'));
	check('a fault nothing reports anymore stays hidden', has('printer is offline') === false);
	live.report('');
}

console.log(out.join('\n'));
console.log(out.some((line) => line.startsWith('FAIL')) ? 'RESULT: failures' : 'RESULT: all pass');
process.exitCode = out.some((line) => line.startsWith('FAIL')) ? 1 : 0;
