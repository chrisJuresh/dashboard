/* Small shared runtime state: theme + a poll helper tied to the sample cadence. */
import { readable, writable } from 'svelte/store';
import { browser } from '$app/environment';

export type Theme = 'dark' | 'light';

function initialTheme(): Theme {
	if (!browser) return 'dark';
	const saved = localStorage.getItem('a3watch.theme') as Theme | null;
	if (saved) return saved;
	return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export const theme = writable<Theme>(initialTheme());
if (browser) {
	theme.subscribe((t) => {
		document.documentElement.setAttribute('data-theme', t);
		localStorage.setItem('a3watch.theme', t);
	});
}
export function toggleTheme() {
	theme.update((t) => (t === 'dark' ? 'light' : 'dark'));
}

/**
 * poll(fn, everyMs) — runs fn immediately, then on an interval, but ONLY while
 * the tab is visible. The dashboard must add near-zero load; when hidden it
 * stops polling entirely so an open-but-backgrounded tab costs nothing.
 */
export function poll<T>(
	fn: () => Promise<T>,
	everyMs: number,
	onData: (v: T) => void,
	onError: (e: unknown) => void
): () => void {
	if (!browser) return () => {};
	let timer: ReturnType<typeof setTimeout> | null = null;
	let stopped = false;

	async function tick() {
		if (stopped) return;
		if (document.visibilityState !== 'visible') {
			schedule();
			return;
		}
		try {
			onData(await fn());
		} catch (e) {
			onError(e);
		}
		schedule();
	}
	function schedule() {
		if (stopped) return;
		timer = setTimeout(tick, everyMs);
	}
	function onVis() {
		if (document.visibilityState === 'visible') {
			if (timer) clearTimeout(timer);
			tick();
		}
	}
	document.addEventListener('visibilitychange', onVis);
	tick();
	return () => {
		stopped = true;
		if (timer) clearTimeout(timer);
		document.removeEventListener('visibilitychange', onVis);
	};
}

/** now() as a store ticking every 30s, for relative timestamps. */
export const nowStore = readable(Date.now(), (set) => {
	if (!browser) return;
	const id = setInterval(() => set(Date.now()), 30000);
	return () => clearInterval(id);
});
