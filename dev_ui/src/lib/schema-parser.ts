export type CommandSchema = {
	name: string;
	properties: Record<string, PropertySchema>;
	required: string[];
};

export type PropertySchema = {
	type: string;
	const?: any;
	properties?: Record<string, PropertySchema>;
	required?: string[];
	items?: PropertySchema;
	$ref?: string;
};

function resolveRef(ref: string, openApiSchema: any): any {
	const parts = ref.split('/');

	if (parts[0] !== '#') {
		return null;
	}

	let current = openApiSchema;

	for (let i = 1; i < parts.length; i++) {
		current = current?.[parts[i]];

		if (current === undefined) {
			return null;
		}
	}

	return current;
}

function resolveProperties(properties: Record<string, any>, openApiSchema: any): Record<string, PropertySchema> {
	const resolved: Record<string, PropertySchema> = {};

	for (const [key, value] of Object.entries(properties)) {
		if (value.$ref !== undefined) {
			const refSchema = resolveRef(value.$ref, openApiSchema);

			if (refSchema !== null) {
				resolved[key] = {
					type: refSchema.type || 'object',
					properties: refSchema.properties !== undefined
						? resolveProperties(refSchema.properties, openApiSchema)
						: undefined,
					required: refSchema.required,
				};
			} else {
				resolved[key] = value;
			}
		} else if (value.properties !== undefined) {
			resolved[key] = {
				...value,
				properties: resolveProperties(value.properties, openApiSchema),
			};
		} else {
			resolved[key] = value;
		}
	}

	return resolved;
}

export function parseCommandSchemas(openApiSchema: any): CommandSchema[] {
	const commandsPath = openApiSchema.paths?.['/api/robot/commands'];

	if (commandsPath === undefined) {
		return [];
	}

	const postMethod = commandsPath.post;

	if (postMethod === undefined) {
		return [];
	}

	const requestBody = postMethod.requestBody;

	if (requestBody === undefined) {
		return [];
	}

	const content = requestBody.content?.['application/json'];

	if (content === undefined) {
		return [];
	}

	const schema = content.schema;

	if (schema === undefined) {
		return [];
	}

	const oneOf = schema.oneOf;

	if (oneOf === undefined) {
		return [];
	}

	const commands: CommandSchema[] = [];

	for (const item of oneOf) {
		const ref = item.$ref;

		if (ref === undefined) {
			continue;
		}

		const schemaName = ref.split('/').pop();

		if (schemaName === undefined) {
			continue;
		}

		const commandSchema = openApiSchema.components?.schemas?.[schemaName];

		if (commandSchema === undefined) {
			continue;
		}

		const commandNameProp = commandSchema.properties?.command;

		if (commandNameProp === undefined) {
			continue;
		}

		const commandName = commandNameProp.const || commandNameProp.default;

		if (commandName === undefined) {
			continue;
		}

		commands.push({
			name: commandName,
			properties: resolveProperties(commandSchema.properties || {}, openApiSchema),
			required: commandSchema.required || [],
		});
	}

	return commands;
}

export function createDefaultValue(property: PropertySchema): any {
	if (property.const !== undefined) {
		return property.const;
	}

	if (property.type === 'string') {
		return '';
	}

	if (property.type === 'number') {
		return 0;
	}

	if (property.type === 'boolean') {
		return false;
	}

	if (property.type === 'object') {
		const obj: Record<string, any> = {};

		if (property.properties !== undefined) {
			for (const [key, value] of Object.entries(property.properties)) {
				obj[key] = createDefaultValue(value);
			}
		}

		return obj;
	}

	if (property.type === 'array') {
		return [];
	}

	return null;
}
