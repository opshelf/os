// Edits and reverts under a group the file does not have: node tests/config-editor.test.mjs
import { UNSET, changedLeaves, pathId, pruneAbsent, setAt } from '../src/lib/config-editor.ts';

const out = [];
const check = (label, ok) => out.push(`${ok ? 'PASS' : 'FAIL'} ${label}`);
const ids = (before, after) => changedLeaves(before, after).map((change) => pathId(change.path));
const json = JSON.stringify;

const original = { tool: { motors: [] }, io: { usb_camera: null } };

const edited = setAt(original, ['tool', 'gripper', 'open_position'], 3);
check('first edit under an absent group is reported at the leaf', json(ids(original, edited)) === json(['tool.gripper.open_position']));

const nullEdited = setAt(original, ['io', 'usb_camera', 'index'], 1);
check('first edit under a null group is reported at the leaf', json(ids(original, nullEdited)) === json(['io.usb_camera.index']));

const twoLeaves = setAt(edited, ['tool', 'gripper', 'close_position'], 1);
const oneReverted = pruneAbsent(setAt(twoLeaves, ['tool', 'gripper', 'open_position'], UNSET), original, ['tool', 'gripper', 'open_position']);
check('reverting one of two leaves keeps the other', json(ids(original, oneReverted)) === json(['tool.gripper.close_position']));

const reverted = pruneAbsent(setAt(edited, ['tool', 'gripper', 'open_position'], UNSET), original, ['tool', 'gripper', 'open_position']);
check('reverting the only leaf leaves no empty group behind', json(reverted) === json(original) && ids(original, reverted).length === 0);

const nullReverted = pruneAbsent(setAt(nullEdited, ['io', 'usb_camera', 'index'], UNSET), original, ['io', 'usb_camera', 'index']);
check('reverting under a null group restores the null', json(nullReverted) === json(original));

const kept = { tool: { gripper: { enabled: true } } };
const keptUnset = pruneAbsent(setAt(kept, ['tool', 'gripper', 'enabled'], UNSET), kept, ['tool', 'gripper', 'enabled']);
check('a group the original had stays when emptied', json(keptUnset) === json({ tool: { gripper: {} } }));

console.log(out.join('\n'));
console.log(out.some((line) => line.startsWith('FAIL')) ? 'RESULT: failures' : 'RESULT: all pass');
process.exitCode = out.some((line) => line.startsWith('FAIL')) ? 1 : 0;
