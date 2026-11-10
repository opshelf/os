// Schema-driven tree model for the config editor page. Pure functions; the page owns state.

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type PathKey = string | number;
export type Schema = Record<string, unknown>;

export type FieldKind = 'group' | 'boolean' | 'integer' | 'number' | 'string' | 'enum' | 'tuple-raw';

export type Field = {
	kind: FieldKind;
	nullable: boolean;
	options: JsonValue[];
	min?: number;
	max?: number;
	minExclusive?: number;
	maxExclusive?: number;
	defaultValue?: JsonValue;
	hasDefault: boolean;
};

export type Row = {
	path: PathKey[];
	id: string;
	key: string;
	label: string;
	depth: number;
	field: Field;
	schema: Schema;
	present: boolean;
	value: JsonValue | undefined;
	required: boolean;
	searchSegments: string[];
	childCount: number;
};

export const UNSET = Symbol('unset');

export function pathId(path: PathKey[]): string {
	return path.join('.');
}

function asSchema(value: unknown): Schema {
	return typeof value === 'object' && value !== null ? (value as Schema) : {};
}

export function resolveSchema(schema: Schema, defs: Record<string, Schema>): Schema {
	let current = schema;
	for (let guard = 0; guard < 10 && typeof current.$ref === 'string'; guard += 1) {
		const name = (current.$ref as string).split('/').pop() ?? '';
		const { $ref: _ref, ...rest } = current;
		current = { ...asSchema(defs[name]), ...rest };
	}
	return current;
}

function numberOr(...values: unknown[]): number | undefined {
	for (const value of values) {
		if (typeof value === 'number') {
			return value;
		}
	}
	return undefined;
}

export function describeField(input: Schema, defs: Record<string, Schema>): { field: Field; schema: Schema } {
	let schema = resolveSchema(input, defs);
	let nullable = false;
	const variants = Array.isArray(schema.anyOf) ? (schema.anyOf as Schema[]).map((s) => resolveSchema(s, defs)) : [];
	if (variants.length > 0) {
		const nonNull = variants.filter((variant) => variant.type !== 'null');
		nullable = nonNull.length < variants.length;
		const types = new Set(nonNull.map((variant) => variant.type));
		const { anyOf: _anyOf, ...outer } = schema;
		if (nonNull.length === 1) {
			schema = { ...nonNull[0], ...outer };
		} else if ([...types].every((type) => type === 'integer' || type === 'number')) {
			schema = { ...outer, type: types.has('number') ? 'number' : 'integer' };
		} else {
			schema = { ...outer, type: 'tuple-raw' };
		}
	}

	// pydantic emits a union's bounds as raw gt/ge/lt/le keywords
	const field: Field = {
		kind: 'string',
		nullable,
		options: [],
		min: numberOr(schema.minimum, schema.ge),
		max: numberOr(schema.maximum, schema.le),
		minExclusive: numberOr(schema.exclusiveMinimum, schema.gt),
		maxExclusive: numberOr(schema.exclusiveMaximum, schema.lt),
		hasDefault: 'default' in schema,
		defaultValue: schema.default as JsonValue | undefined
	};

	if (Array.isArray(schema.enum)) {
		field.kind = 'enum';
		field.options = schema.enum as JsonValue[];
	} else if (schema.type === 'boolean') {
		field.kind = 'boolean';
	} else if (schema.type === 'integer') {
		field.kind = 'integer';
	} else if (schema.type === 'number') {
		field.kind = 'number';
	} else if (schema.type === 'object' || schema.type === 'array' || 'properties' in schema) {
		field.kind = 'group';
	} else if (schema.type === 'tuple-raw') {
		field.kind = 'tuple-raw';
	}
	return { field, schema };
}

function inferField(value: JsonValue | undefined): Field {
	const kind: FieldKind =
		value !== null && typeof value === 'object'
			? 'group'
			: typeof value === 'boolean'
				? 'boolean'
				: typeof value === 'number'
					? 'number'
					: 'string';
	return { kind, nullable: value === null, options: [], hasDefault: false };
}

// A list item is addressed by its name in search ("motors.stage.lim") when it has one.
function itemName(value: JsonValue | undefined): string | null {
	if (value !== null && typeof value === 'object' && Array.isArray(value) === false) {
		for (const key of ['motor_name', 'name']) {
			const name = (value as Record<string, JsonValue>)[key];
			if (typeof name === 'string' && name !== '') {
				return name;
			}
		}
	}
	return null;
}

function childEntries(
	schema: Schema,
	value: JsonValue | undefined
): Array<{ key: PathKey; schema: Schema; required: boolean }> {
	const out: Array<{ key: PathKey; schema: Schema; required: boolean }> = [];
	if (Array.isArray(value)) {
		const prefix = Array.isArray(schema.prefixItems) ? (schema.prefixItems as Schema[]) : [];
		const items = asSchema(schema.items);
		value.forEach((_, index) => out.push({ key: index, schema: prefix[index] ?? items, required: true }));
		return out;
	}
	const properties = asSchema(schema.properties) as Record<string, Schema>;
	const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
	const seen = new Set<string>();
	for (const [key, child] of Object.entries(properties)) {
		seen.add(key);
		out.push({ key, schema: child, required: required.has(key) });
	}
	const extra = asSchema(schema.additionalProperties);
	if (value !== null && typeof value === 'object') {
		for (const key of Object.keys(value)) {
			if (seen.has(key) === false) {
				out.push({ key, schema: extra, required: false });
			}
		}
	}
	return out;
}

export function buildRows(data: JsonValue | undefined, rootSchema: Schema, defs: Record<string, Schema>): Row[] {
	const rows: Row[] = [];

	function walk(
		path: PathKey[],
		segments: string[],
		inputSchema: Schema,
		value: JsonValue | undefined,
		present: boolean,
		required: boolean
	): void {
		const described = describeField(inputSchema, defs);
		const field = Object.keys(inputSchema).length === 0 ? inferField(value) : described.field;
		const schema = described.schema;
		const key = path.length === 0 ? '' : String(path[path.length - 1]);
		const name = itemName(value);
		const children = field.kind === 'group' ? childEntries(schema, value) : [];
		if (path.length > 0) {
			rows.push({
				path,
				id: pathId(path),
				key,
				label: name !== null && typeof path[path.length - 1] === 'number' ? `${key} ${name}` : key,
				depth: path.length - 1,
				field,
				schema,
				present,
				value,
				required,
				searchSegments: segments,
				childCount: children.length
			});
		}
		for (const child of children) {
			const container = value !== null && typeof value === 'object' ? (value as Record<string, JsonValue>) : undefined;
			const childValue = container === undefined ? undefined : (container as Record<string, JsonValue>)[child.key as string];
			const childPresent = present && container !== undefined && child.key in container;
			const childName = typeof child.key === 'number' ? itemName(childValue) : null;
			walk(
				[...path, child.key],
				[...segments, childName ?? String(child.key)],
				child.schema,
				childValue,
				childPresent,
				child.required
			);
		}
	}

	walk([], [], rootSchema, data, data !== undefined, true);
	return rows;
}

// --- search -------------------------------------------------------------------------------

function segmentMatches(segment: string, query: string, fuzzy: boolean): boolean {
	if (segment.includes(query)) {
		return true;
	}
	if (fuzzy === false) {
		return false;
	}
	let index = 0;
	for (const char of segment) {
		if (char === query[index]) {
			index += 1;
			if (index === query.length) {
				return true;
			}
		}
	}
	return false;
}

/** Each dot-separated query part must match a path segment (substring, or in-order letters when
 *  `fuzzy`), parts in order, segments may be skipped. "pins.pin" matches gpio.pins.<name>.pin. */
export function pathMatches(segments: string[], query: string, fuzzy = false): boolean {
	const parts = query
		.toLowerCase()
		.split('.')
		.map((part) => part.trim())
		.filter((part) => part !== '');
	if (parts.length === 0) {
		return true;
	}
	let at = 0;
	for (const part of parts) {
		while (at < segments.length && segmentMatches(segments[at].toLowerCase(), part, fuzzy) === false) {
			at += 1;
		}
		if (at === segments.length) {
			return false;
		}
		at += 1;
	}
	return true;
}

/** Rows that match, their ancestors (context) and the descendants of a matching group. Substring
 *  matches win; in-order letter matching ("stgext") is the fallback when nothing matches. */
export function filterRows(rows: Row[], query: string): Set<string> {
	const exact = collectMatches(rows, query, false);
	return exact.size > 0 ? exact : collectMatches(rows, query, true);
}

function collectMatches(rows: Row[], query: string, fuzzy: boolean): Set<string> {
	const visible = new Set<string>();
	const matchedGroups: string[] = [];
	for (const row of rows) {
		if (matchedGroups.some((prefix) => row.id.startsWith(prefix))) {
			visible.add(row.id);
			continue;
		}
		if (pathMatches(row.searchSegments, query, fuzzy)) {
			visible.add(row.id);
			for (let depth = 1; depth < row.path.length; depth += 1) {
				visible.add(pathId(row.path.slice(0, depth)));
			}
			if (row.field.kind === 'group') {
				matchedGroups.push(`${row.id}.`);
			}
		}
	}
	return visible;
}

// --- values -------------------------------------------------------------------------------

export function getAt(data: JsonValue | undefined, path: PathKey[]): JsonValue | undefined {
	let node: JsonValue | undefined = data;
	for (const key of path) {
		if (node === null || typeof node !== 'object') {
			return undefined;
		}
		node = (node as Record<string, JsonValue>)[key as string];
	}
	return node;
}

/** Returns a copy of `data` with the value at `path` set (or removed with UNSET), creating
 *  missing objects on the way. */
export function setAt(data: JsonValue, path: PathKey[], value: JsonValue | typeof UNSET): JsonValue {
	if (path.length === 0) {
		return value === UNSET ? {} : value;
	}
	const [head, ...rest] = path;
	const container: Record<string, JsonValue> | JsonValue[] = Array.isArray(data)
		? [...data]
		: data !== null && typeof data === 'object'
			? { ...data }
			: {};
	const current = (container as Record<string, JsonValue>)[head as string];
	if (rest.length === 0) {
		if (value === UNSET) {
			if (Array.isArray(container) === false) {
				delete (container as Record<string, JsonValue>)[head as string];
			}
		} else {
			(container as Record<string, JsonValue>)[head as string] = value;
		}
		return container as JsonValue;
	}
	(container as Record<string, JsonValue>)[head as string] = setAt(current ?? {}, rest, value);
	return container as JsonValue;
}

/** After a leaf was removed from `data`, drops ancestor objects left empty that `original` did not
 *  have as an object (restoring a null the original held), so reverting under an absent group
 *  does not leave `{}` behind. */
export function pruneAbsent(data: JsonValue, original: JsonValue | undefined, path: PathKey[]): JsonValue {
	let next = data;
	for (let depth = path.length - 1; depth >= 1; depth -= 1) {
		const parent = path.slice(0, depth);
		const node = getAt(next, parent);
		const was = getAt(original, parent);
		const isEmpty = node !== null && typeof node === 'object' && Array.isArray(node) === false && Object.keys(node).length === 0;
		const wasObject = was !== null && typeof was === 'object';
		if (isEmpty === false || wasObject) {
			break;
		}
		next = setAt(next, parent, was === undefined ? UNSET : was);
	}
	return next;
}

export function sameValue(a: JsonValue | undefined, b: JsonValue | undefined): boolean {
	return JSON.stringify(a) === JSON.stringify(b);
}

/** Leaf paths whose values differ between two documents, with before/after values. */
export function changedLeaves(
	before: JsonValue | undefined,
	after: JsonValue | undefined,
	path: PathKey[] = []
): Array<{ path: PathKey[]; before: JsonValue | undefined; after: JsonValue | undefined }> {
	const isObject = (v: JsonValue | undefined) => v !== null && typeof v === 'object' && v !== undefined;
	// a group missing (or null) in one document is compared leaf by leaf against the other side
	const isFilled = (v: JsonValue | undefined) => isObject(v) && Array.isArray(v) === false && Object.keys(v as object).length > 0;
	if ((before === undefined || before === null) && isFilled(after)) {
		before = {};
	} else if ((after === undefined || after === null) && isFilled(before)) {
		after = {};
	}
	if (isObject(before) && isObject(after) && Array.isArray(before) === Array.isArray(after)) {
		const keys = new Set([...Object.keys(before as object), ...Object.keys(after as object)]);
		return [...keys].flatMap((key) =>
			changedLeaves(
				(before as Record<string, JsonValue>)[key],
				(after as Record<string, JsonValue>)[key],
				[...path, Array.isArray(before) ? Number(key) : key]
			)
		);
	}
	return sameValue(before, after) ? [] : [{ path, before, after }];
}

export function formatValue(value: JsonValue | undefined): string {
	if (value === undefined) {
		return 'unset';
	}
	return JSON.stringify(value);
}

export function boundsHint(field: Field): string {
	const parts: string[] = [];
	if (field.min !== undefined) parts.push(`≥ ${field.min}`);
	if (field.minExclusive !== undefined) parts.push(`> ${field.minExclusive}`);
	if (field.max !== undefined) parts.push(`≤ ${field.max}`);
	if (field.maxExclusive !== undefined) parts.push(`< ${field.maxExclusive}`);
	return parts.join(' ');
}

/** Immediate check of a typed value against the schema bounds; the server has the final word. */
export function localIssue(field: Field, value: JsonValue | undefined): string | null {
	if (value === undefined) {
		return null;
	}
	if (value === null) {
		return field.nullable ? null : 'A value is required.';
	}
	if (field.kind === 'integer' || field.kind === 'number') {
		if (typeof value !== 'number' || Number.isFinite(value) === false) {
			return 'Enter a number.';
		}
		if (field.kind === 'integer' && Number.isInteger(value) === false) {
			return 'Enter a whole number.';
		}
		if (field.min !== undefined && value < field.min) return `Must be ≥ ${field.min}.`;
		if (field.minExclusive !== undefined && value <= field.minExclusive) return `Must be > ${field.minExclusive}.`;
		if (field.max !== undefined && value > field.max) return `Must be ≤ ${field.max}.`;
		if (field.maxExclusive !== undefined && value >= field.maxExclusive) return `Must be < ${field.maxExclusive}.`;
	}
	return null;
}

export function isRegisterField(key: string): boolean {
	return /(_reg|address|output_function)$/.test(key);
}
