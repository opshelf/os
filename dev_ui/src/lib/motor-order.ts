export type MotorMove = { name: string; positionMm: number };

// A tray on the tool can carry an item: Z leaves before the stage on the way in, the stage goes
// out before Z on the way out, and the gantry only moves with both pulled in.
export function orderSectionMoves(
	moves: MotorMove[],
	currentMm: Record<string, number | undefined>
): MotorMove[] | { unknown: string } {
	const retracts: MotorMove[] = [];
	const others: MotorMove[] = [];
	const extends_: MotorMove[] = [];

	for (const name of ['z_axis', 'stage']) {
		const move = moves.find((m) => m.name === name);

		if (move === undefined) {
			continue;
		}

		const current = currentMm[name];

		if (current === undefined) {
			return { unknown: name };
		}

		(move.positionMm > current ? extends_ : retracts).push(move);
	}

	for (const move of moves) {
		if (move.name !== 'z_axis' && move.name !== 'stage') {
			others.push(move);
		}
	}

	return [...retracts, ...others, ...extends_.reverse()];
}
