import { writable } from 'svelte/store';
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

// Development and mock devices keep this PIN so the UI signs in by itself.
// Production and demo devices reject it and have their own, which the UI asks
// for once and keeps in memory for the tab.
const DEFAULT_DEVELOPER_PIN = '1111';

let developerPin: string | null = null;
let developerSignIn: Promise<void> | null = null;
let resolvePinRequest: ((pin: string) => void) | null = null;

export type DeveloperPinRequest = { error: string };

// Non-null while the UI must ask for the developer PIN; the layout renders the prompt.
export const developerPinRequest = writable<DeveloperPinRequest | null>(null);

export function submitDeveloperPin(pin: string): void {
	const resolve = resolvePinRequest;
	resolvePinRequest = null;
	developerPinRequest.set(null);
	resolve?.(pin);
}

function askForDeveloperPin(error: string): Promise<string> {
	return new Promise((resolve) => {
		resolvePinRequest = resolve;
		developerPinRequest.set({ error });
	});
}

async function signInAsDeveloper(): Promise<void> {
	let pin = developerPin ?? DEFAULT_DEVELOPER_PIN;
	let error = '';

	for (;;) {
		try {
			await login(DEVELOPER_EMAIL, pin);
			developerPin = pin;
			return;
		} catch (err) {
			if (err instanceof LoginRejectedError === false) {
				throw err;
			}
		}

		developerPin = null;
		pin = await askForDeveloperPin(error);
		error = 'wrong pin';
	}
}

// The api client, the status stream, and the layout all sign in on demand, so
// concurrent callers share one attempt and at most one prompt is on screen.
export function loginAsDeveloper(): Promise<void> {
	if (developerSignIn === null) {
		developerSignIn = signInAsDeveloper().finally(() => {
			developerSignIn = null;
		});
	}
	return developerSignIn;
}
