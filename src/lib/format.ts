/* Shared formatters + series-slot mapping (fixed order, never cycled). */

export function fmtBytes(n: number): string {
	if (!isFinite(n) || n <= 0) return '0 B';
	const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
	const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
	return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtWatts(w: number): string {
	if (!isFinite(w)) return '—';
	return `${w.toFixed(w < 10 ? 2 : 1)} W`;
}

export function fmtGbp(v: number): string {
	if (!isFinite(v)) return '—';
	return `£${v.toFixed(2)}`;
}

export function fmtPct(v: number): string {
	if (!isFinite(v)) return '—';
	return `${v.toFixed(v < 10 ? 1 : 0)}%`;
}

export function fmtDuration(mins: number): string {
	if (!isFinite(mins) || mins < 0) return '—';
	if (mins < 60) return `${Math.round(mins)}m`;
	const h = Math.floor(mins / 60);
	if (h < 24) return `${h}h ${Math.round(mins % 60)}m`;
	const d = Math.floor(h / 24);
	return `${d}d ${h % 24}h`;
}

export function fmtClock(ts: number): string {
	return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function fmtDateTime(ts: number): string {
	return new Date(ts * 1000).toLocaleString([], {
		month: 'short',
		day: 'numeric',
		hour: '2-digit',
		minute: '2-digit'
	});
}

export function fmtRelative(ts: number): string {
	const s = Date.now() / 1000 - ts;
	if (s < 60) return `${Math.round(s)}s ago`;
	if (s < 3600) return `${Math.round(s / 60)}m ago`;
	if (s < 86400) return `${Math.round(s / 3600)}h ago`;
	return `${Math.round(s / 86400)}d ago`;
}

/** Disk power-state → status role (good=asleep is desirable for HDDs). */
export function diskStateStatus(state: string, rotational: boolean): 'good' | 'warning' | 'muted' {
	if (!rotational) return 'muted'; // NVMe/SSD: sleep state N/A
	if (state === 'standby' || state === 'sleeping') return 'good';
	if (state === 'active') return 'warning';
	return 'muted';
}

export function confidenceStatus(c: string): 'good' | 'warning' | 'serious' {
	return c === 'high' ? 'good' : c === 'medium' ? 'warning' : 'serious';
}

/** Fixed categorical slot for a series key (stable across filters). */
const SLOTS = 8;
export function seriesVar(index: number): string {
	return `var(--series-${(index % SLOTS) + 1})`;
}
