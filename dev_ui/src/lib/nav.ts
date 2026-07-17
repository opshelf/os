const DEVICE_RE = /^\/([0-9a-f]{8})(\/.*)?$/;

function deviceIdFromLocation(): string | null {
	if (typeof location === 'undefined') {
		return null;
	}

	const match = location.pathname.match(DEVICE_RE);

	if (match !== null) {
		return match[1];
	}

	try {
		const stored = sessionStorage.getItem('deviceId');

		if (stored !== null && /^[0-9a-f]{8}$/.test(stored) === true) {
			return stored;
		}
	} catch {
		return null;
	}

	return null;
}

export function devicePath(path: string): string {
	const id = deviceIdFromLocation();

	if (id === null) {
		return path;
	}

	if (path === '/') {
		return `/${id}`;
	}

	return `/${id}${path}`;
}

export function stripDeviceId(pathname: string): string {
	const match = pathname.match(DEVICE_RE);

	if (match === null) {
		return pathname;
	}

	return match[2] ?? '/';
}
