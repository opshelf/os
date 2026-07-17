import type { Reroute } from '@sveltejs/kit';

const DEVICE_RE = /^\/([0-9a-f]{8})(\/.*)?$/;

export const reroute: Reroute = ({ url }) => {
	const match = url.pathname.match(DEVICE_RE);

	if (match === null) {
		return;
	}

	try {
		sessionStorage.setItem('deviceId', match[1]);
	} catch {
		return match[2] ?? '/';
	}

	return match[2] ?? '/';
};
