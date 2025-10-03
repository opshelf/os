const STORAGE_KEY = 'robot_recent_commands';
const MAX_RECENT = 5;

export type RecentCommand = {
	command: any;
	timestamp: number;
};

export function getRecentCommands(): RecentCommand[] {
	const stored = localStorage.getItem(STORAGE_KEY);

	if (stored === null) {
		return [];
	}

	try {
		return JSON.parse(stored);
	} catch {
		return [];
	}
}

export function addRecentCommand(command: any): void {
	const recent = getRecentCommands();
	const newCommand: RecentCommand = {
		command,
		timestamp: Date.now(),
	};

	recent.unshift(newCommand);

	const trimmed = recent.slice(0, MAX_RECENT);

	localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
}

export function clearRecentCommands(): void {
	localStorage.removeItem(STORAGE_KEY);
}
