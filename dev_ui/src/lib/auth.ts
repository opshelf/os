import { getApiBase } from "./api";

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

export async function login(email: string, pin: string): Promise<void> {
	const apiBase = getApiBase();
	const url = `${apiBase}/auth/login/json`;

	const response = await fetch(url, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ email, pin }),
	});

	if (response.ok === false) {
		throw new Error("login failed");
	}

	const data = (await response.json()) as TokenResponse;

	accessToken = data.access_token;
	refreshToken = data.refresh_token;
	tokenExpiresAt = Date.now() + data.expires_in * 1000;
}

export async function refreshAccessToken(): Promise<void> {
	if (refreshToken === null) {
		throw new Error("no refresh token available");
	}

	const apiBase = getApiBase();
	const url = `${apiBase}/auth/refresh`;

	const response = await fetch(url, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ refresh_token: refreshToken }),
	});

	if (response.ok === false) {
		accessToken = null;
		refreshToken = null;
		tokenExpiresAt = null;
		throw new Error("token refresh failed");
	}

	const data = (await response.json()) as TokenResponse;

	accessToken = data.access_token;
	tokenExpiresAt = Date.now() + data.expires_in * 1000;
}

export async function ensureValidToken(): Promise<string> {
	if (accessToken === null) {
		throw new Error("not authenticated");
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

export async function loginAsDeveloper(): Promise<void> {
	await login("developer@opshelf.com", "1111");
}
