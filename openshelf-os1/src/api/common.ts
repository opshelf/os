// In development, use the same hostname as the UI but on port 8000
// In production, use relative path /api
export const HOST = import.meta.env.DEV 
	? `http://${window.location.hostname}:8000/api` 
	: "/api";
