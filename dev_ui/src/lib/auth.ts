import { getApiBase } from './api';

interface TokenResponse {
	access_token: string;
	refresh_token: string;
	token_type: string;
	expires_in: number;
}

let accessToken: string | null = null;
let refreshToken: string | null = null;
let tokenExpiresAt: number | null = null;

export function getAccessToken(): string | null {
	return accessToken;
}

export function isAuthenticated(): boolean {
	return accessToken !== null;
}

export class LoginRejectedError extends Error {}

export async function login(email: string, pin: string): Promise<void> {
	const apiBase = getApiBase();
	const url = `${apiBase}/auth/login/json`;

	const response = await fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ email, pin })
	});

	if (response.status === 401) {
		throw new LoginRejectedError('login rejected');
	}

	if (response.ok === false) {
		throw new Error('login failed');
	}

	const data = (await response.json()) as TokenResponse;

	accessToken = data.access_token;
	refreshToken = data.refresh_token;
	tokenExpiresAt = Date.now() + data.expires_in * 1000;
}

export async function refreshAccessToken(): Promise<void> {
	if (refreshToken === null) {
		throw new Error('no refresh token available');
	}

	const apiBase = getApiBase();
	const url = `${apiBase}/auth/refresh`;

	const response = await fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ refresh_token: refreshToken })
	});

	if (response.ok === false) {
		accessToken = null;
		refreshToken = null;
		tokenExpiresAt = null;
		throw new Error('token refresh failed');
	}

	const data = (await response.json()) as TokenResponse;

	accessToken = data.access_token;
	tokenExpiresAt = Date.now() + data.expires_in * 1000;
}

export async function ensureValidToken(): Promise<string> {
	if (accessToken === null) {
		throw new Error('not authenticated');
	}

	if (tokenExpiresAt !== null && Date.now() > tokenExpiresAt - 60000) {
		await refreshAccessToken();
	}

	return accessToken;
}

export function logout(): void {
	accessToken = null;
	refreshToken = null;
	tokenExpiresAt = null;
}

const DEVELOPER_EMAIL = 'developer@opshelf.com';
const DEVELOPER_PIN_COOKIE = 'opshelf_developer_pin';

// The relay serving dev.opshelf.com sets the developer PIN as a cookie, so the PIN
// never lives in this public repository and the UI never asks for it. Without the
// cookie (a development or mock device reached directly) the API serves the
// developer user without a token, so there is nothing to sign in to.
export function developerPin(): string | null {
	for (const part of document.cookie.split(';')) {
		const [name, ...value] = part.trim().split('=');
		if (name === DEVELOPER_PIN_COOKIE) {
			const pin = decodeURIComponent(value.join('='));
			return pin === '' ? null : pin;
		}
	}
	return null;
}

let developerSignIn: Promise<void> | null = null;

async function signInAsDeveloper(): Promise<void> {
	const pin = developerPin();
	if (pin === null) {
		throw new Error('no developer pin cookie');
	}
	await login(DEVELOPER_EMAIL, pin);
}

// The api client, the status stream, and the layout all sign in on demand, so
// concurrent callers share one attempt.
export function loginAsDeveloper(): Promise<void> {
	if (developerSignIn === null) {
		developerSignIn = signInAsDeveloper().finally(() => {
			developerSignIn = null;
		});
	}
	return developerSignIn;
}
