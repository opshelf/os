// Prefer matching the UI host in dev so hitting the UI through an IP keeps the
// API reachable. Allow overriding via Vite env for setups that require a
// specific host.
const DEV_HOST_OVERRIDE = import.meta.env.VITE_API_HOST?.trim();
const DEV_HOST_DEFAULT = `http://${window.location.hostname}:8000/api`;

export const HOST = import.meta.env.DEV
	? DEV_HOST_OVERRIDE || DEV_HOST_DEFAULT
	: "/api";
