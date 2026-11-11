import json

from openshelf_os_server.main import app
from openshelf_os_server.openapi_checks import validate_document


def test_exported_schema_passes_contract_checks_and_documents_shared_422_body() -> None:
    document = json.loads(json.dumps(app.openapi()))
    validate_document(document)
    assert "HTTPValidationError" not in document["components"]["schemas"]
    for item in document["paths"].values():
        for operation in item.values():
            response = operation.get("responses", {}).get("422") if isinstance(operation, dict) else None
            if response is not None:
                assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}
