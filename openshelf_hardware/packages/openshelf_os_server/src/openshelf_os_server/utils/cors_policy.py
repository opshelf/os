"""Cross-origin policy for the local API.

The browser-served UI runs on its own port, so the API has to answer cross-origin
requests. Authentication travels in an `Authorization` header or a `token` query
parameter and never in a cookie, so a wildcard origin must not be published together
with credentialed access: the combination is invalid in browsers, and reflecting the
caller's origin instead would let any website that reaches the device read responses
to requests the browser sent with credentials.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

WILDCARD_ORIGIN = "*"
CORS_ORIGINS_ENV = "OPENSHELF_CORS_ORIGINS"


def resolve_allowed_origins(raw: Optional[str] = None) -> list[str]:
	"""Return the configured origins, defaulting to any origin.

	`OPENSHELF_CORS_ORIGINS` is a comma-separated list of exact origins such as
	`http://device.local:8080`. It is environment-only: the origin list is deployment
	wiring, so pinning it never needs a config schema migration.
	"""
	value = raw if raw is not None else os.environ.get(CORS_ORIGINS_ENV, "")
	origins = [entry.strip() for entry in value.split(",") if entry.strip()]
	return origins or [WILDCARD_ORIGIN]


def allows_credentials(origins: Sequence[str]) -> bool:
	"""Report whether the policy may advertise credentialed cross-origin access."""
	return WILDCARD_ORIGIN not in origins


def response_allow_origin(origins: Sequence[str], origin: Optional[str]) -> Optional[str]:
	"""Origin header value for a response built outside the CORS middleware.

	Responses the audit middleware produces on its own never travel back through that
	middleware, so they have to carry the header themselves. Returns `None` when the
	policy must not expose the response to this caller.
	"""
	if origin is None:
		return None
	if WILDCARD_ORIGIN in origins:
		return WILDCARD_ORIGIN
	return origin if origin in origins else None
