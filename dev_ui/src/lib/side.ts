// Terminology-agnostic side handling. The physical identity of a storage module
// is its stable s-index (0 and 1); the *name* ("left"/"right" vs "front"/
// "back") is a device vocabulary that changed across the module-side rename.
// The UI keys all logic off the s-index and infers the active vocabulary from
// the device's own data (the storage grid's keys), so one build works against
// devices that speak either vocabulary.

export type SideIndex = 0 | 1;
export type SideVocabulary = 'left_right' | 'front_back';

// Canonical internal tokens. They double as the UI's own state (URL params,
// localStorage, component state) and are translated to the device vocabulary at
// the API boundary.
export const CANONICAL_SIDES = ['front_module', 'back_module'] as const;
export type CanonicalSide = (typeof CANONICAL_SIDES)[number];

// Every token a device, URL, or stored selection may use, by s-index.
const SIDE_TOKEN_INDEX: Record<string, SideIndex> = {
	left_module: 0,
	front_module: 0,
	left: 0,
	front: 0,
	l: 0,
	f: 0,
	right_module: 1,
	back_module: 1,
	right: 1,
	back: 1,
	r: 1,
	b: 1,
};

export function sideIndex(token: unknown): SideIndex | null {
	if (typeof token !== 'string') {
		return null;
	}
	return SIDE_TOKEN_INDEX[token.trim().toLowerCase()] ?? null;
}

export function isSideToken(token: unknown): boolean {
	return sideIndex(token) !== null;
}

/** Which vocabulary a single token belongs to (null when it is not a side token). */
export function vocabularyOfToken(token: unknown): SideVocabulary | null {
	if (typeof token !== 'string') {
		return null;
	}
	const lower = token.trim().toLowerCase();
	if (['left_module', 'left', 'l', 'right_module', 'right', 'r'].includes(lower)) {
		return 'left_right';
	}
	if (['front_module', 'front', 'f', 'back_module', 'back', 'b'].includes(lower)) {
		return 'front_back';
	}
	return null;
}

/**
 * Infer the active vocabulary from the storage grid's keys (the authoritative
 * per-device evidence: a v4 grid speaks left/right, a v5 grid front/back).
 * Returns null when the evidence names neither vocabulary.
 */
export function inferVocabularyFromGrid(grid: Record<string, unknown> | null | undefined): SideVocabulary | null {
	if (grid === null || grid === undefined) {
		return null;
	}
	if ('front_module' in grid || 'back_module' in grid) {
		return 'front_back';
	}
	if ('left_module' in grid || 'right_module' in grid) {
		return 'left_right';
	}
	return null;
}

export function sideToken(index: SideIndex, vocabulary: SideVocabulary): string {
	if (vocabulary === 'front_back') {
		return index === 0 ? 'front_module' : 'back_module';
	}
	return index === 0 ? 'left_module' : 'right_module';
}

export function sideLabel(index: SideIndex, vocabulary: SideVocabulary): string {
	if (vocabulary === 'front_back') {
		return index === 0 ? 'Front' : 'Back';
	}
	return index === 0 ? 'Left' : 'Right';
}

export function sideInitial(index: SideIndex, vocabulary: SideVocabulary): string {
	return sideLabel(index, vocabulary)[0];
}

// The active vocabulary, inferred from device data. Defaults to the current
// release vocabulary until the first grid response says otherwise; the first
// grid fetch on a side-aware page always precedes the side-specific UI content.
let active: SideVocabulary = 'front_back';
const subscribers = new Set<(value: SideVocabulary) => void>();

export function setActiveVocabulary(vocabulary: SideVocabulary): void {
	if (vocabulary === active) {
		return;
	}
	active = vocabulary;
	for (const notify of subscribers) {
		notify(active);
	}
}

export function activeVocabulary(): SideVocabulary {
	return active;
}

/** Svelte store subscription; the store value is the current vocabulary. */
export const activeVocabularyStore = {
	subscribe(onChange: (value: SideVocabulary) => void): () => void {
		subscribers.add(onChange);
		onChange(active);
		return () => {
			subscribers.delete(onChange);
		};
	}
};

/** Translate a canonical UI token to the token the active device vocabulary speaks. */
export function toDeviceSide(canonical: CanonicalSide): string {
	return sideToken(sideIndex(canonical) as SideIndex, active);
}
