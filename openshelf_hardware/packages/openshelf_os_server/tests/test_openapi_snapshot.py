from openshelf_os_server.openapi import build_openapi_document, openapi_document_is_current


def test_openapi_snapshot_matches_mounted_routes() -> None:
	assert openapi_document_is_current() is True, (
		"OpenAPI snapshot is stale; run `uv run openapi` from openshelf_hardware"
	)


def test_openapi_describes_actual_bearer_authentication() -> None:
	document = build_openapi_document()
	security_schemes = document["components"]["securitySchemes"]

	assert security_schemes == {
		"BearerAuth": {
			"type": "http",
			"scheme": "bearer",
			"bearerFormat": "JWT",
		}
	}


def test_openapi_documents_storage_item_upsert_responses() -> None:
	document = build_openapi_document()
	responses = document["paths"]["/api/storage/items"]["post"]["responses"]

	assert responses["200"]["content"]["application/json"]["schema"] == {
		"$ref": "#/components/schemas/ItemWithLocations"
	}
	assert responses["201"]["content"]["application/json"]["schema"] == {
		"$ref": "#/components/schemas/ItemWithLocations"
	}


def test_openapi_documents_empty_canonical_deletes() -> None:
	document = build_openapi_document()

	for path in (
		"/api/robot/commands",
		"/api/storage/grid/shelf",
		"/api/storage/grid/cell",
	):
		responses = document["paths"][path]["delete"]["responses"]
		assert responses["204"] == {"description": "Successful Response"}
		assert "200" not in responses

	assert "/api/robot/clear_commands" not in document["paths"]
