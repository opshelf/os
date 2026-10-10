"""Contract quality and transport declarations; no robot lifespan is started."""

from fastapi.routing import APIRoute, iter_route_contexts

from openshelf_os_server.main import app
from openshelf_os_server.openapi import generate_openapi_json
from openshelf_os_server.openapi_checks import validate_document


def test_generated_document_matches_live_routes():
    document = generate_openapi_json()
    assert document == app.openapi()
    # app.routes nests included routers (FastAPI 0.143), so walk their effective routes.
    registered = {
        (route.path, method.lower())
        for route in iter_route_contexts(app.routes)
        if isinstance(route.original_route, APIRoute) and route.include_in_schema
        for method in route.methods or ()
    }
    published = {(path, method) for path, item in document["paths"].items() for method in item}
    assert registered == published
    validate_document(document)
