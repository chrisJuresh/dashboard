/*
 * a3watch API client. The dashboard is a static SPA served by Vercel; it talks
 * to the a3watch agent running on the server, reached over the cloudflared
 * tunnel. The API base URL + bearer token are entered once on the connect
 * screen and kept in localStorage. Every call is read-only except the
 * explicit /diag/* endpoints.
 */

const LS_BASE = 'a3watch.apiBase';
const LS_TOKEN = 'a3watch.token';

// Build-time defaults for zero-entry auto-connect (optional, set as Vercel env vars).
// The URL is not secret. VITE_A3WATCH_TOKEN is inlined into the client bundle, so only
// set it when the deployment is protected by Vercel Authentication — otherwise anyone
// who loads the site can read it. Precedence: localStorage (user-set) > env > default.
const ENV_BASE = (import.meta.env.VITE_A3WATCH_API ?? '').replace(/\/+$/, '');
const ENV_TOKEN = import.meta.env.VITE_A3WATCH_TOKEN ?? '';
// Default is SAME-ORIGIN (''): the agent serves this SPA and its /api behind Cloudflare
// Access, so no URL or token is entered — Access handles login and the API is relative.
// An absolute base (env var or the connect screen) switches to remote/bearer-token mode.
const DEFAULT_BASE = '';

export function getApiBase(): string {
	if (typeof localStorage !== 'undefined') {
		const v = localStorage.getItem(LS_BASE);
		if (v) return v;
	}
	return ENV_BASE || DEFAULT_BASE;
}
export function getToken(): string {
	if (typeof localStorage !== 'undefined') {
		const v = localStorage.getItem(LS_TOKEN);
		if (v) return v;
	}
	return ENV_TOKEN;
}
/** True when calls hit the same origin (served by the agent behind Cloudflare Access). */
export function isSameOrigin(): boolean {
	return getApiBase() === '';
}
export function setConnection(base: string, token: string): void {
	localStorage.setItem(LS_BASE, base.replace(/\/+$/, ''));
	localStorage.setItem(LS_TOKEN, token);
}
export function clearConnection(): void {
	localStorage.removeItem(LS_BASE);
	localStorage.removeItem(LS_TOKEN);
}
// Same-origin mode is always "configured" (Cloudflare Access gates it, no token needed).
// Remote mode needs a bearer token; otherwise show the connect screen.
export function isConfigured(): boolean {
	return isSameOrigin() || getToken() !== '';
}

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
	// base is '' in same-origin mode → a relative fetch to the agent that served
	// this page (gated by Cloudflare Access). An absolute base = remote/bearer mode.
	const base = getApiBase();
	const headers = new Headers(init?.headers);
	const token = getToken();
	if (token) headers.set('Authorization', `Bearer ${token}`);
	let resp: Response;
	try {
		resp = await fetch(`${base}${path}`, { ...init, headers });
	} catch (e) {
		throw new ApiError(0, 'unreachable');
	}
	if (!resp.ok) {
		let msg = resp.statusText;
		try {
			const body = await resp.json();
			if (body?.error) msg = body.error;
		} catch {
			/* ignore */
		}
		throw new ApiError(resp.status, msg);
	}
	return (await resp.json()) as T;
}

// ---- types (mirror agent/CONTRACT.md) --------------------------------------
export type Mode = 'normal' | 'diagnostic';
export type Confidence = 'high' | 'medium' | 'low';
export type PowerState = 'active' | 'idle' | 'standby' | 'sleeping' | 'unknown';

export interface DiskStatus {
	dev: string;
	role: string;
	model: string;
	mount: string;
	label: string;
	rotational: boolean;
	protected: boolean;
	power_state: PowerState;
	active: boolean;
	minutes_in_state: number;
	reads_recent: number;
	writes_recent: number;
}
export interface CState {
	name: string;
	pct: number;
}
export interface Status {
	ts: number;
	mode: Mode;
	disks: DiskStatus[];
	cpu: {
		pkg_w: number;
		core_w: number;
		busy_pct: number;
		pkg_cstates: CState[];
		core_cstates: CState[];
		pkg_deep_ok: boolean;
	};
	overhead: {
		avg_watts: number;
		gbp_year: number;
		budget_gbp: number;
		db_mb: number;
		samples: number;
		cpu_ms_day: number;
		within_budget: boolean;
	};
	counts: { open_disk_events: number; stray_procs: number };
}

export interface Evidence {
	signal: string;
	detail: string;
	weight: number;
}
export interface DiskEvent {
	id: number;
	ts: number;
	dev: string;
	kind: 'spinup' | 'write' | 'read' | 'stay_awake';
	confidence: Confidence;
	primary_cause: string;
	cause_kind: string;
	note: string;
	evidence: Evidence[];
}
export interface PowerEvent {
	id: number;
	ts: number;
	kind: 'watt_rise' | 'pkg_cstate_stall';
	confidence: Confidence;
	primary_cause: string;
	detail: string;
}
export interface ProcInfo {
	pid: number;
	comm: string;
	cgroup: string;
	cpu_pct: number;
	read_bytes_d: number;
	write_bytes_d: number;
	flags: string[];
	note: string;
}
export interface PowerPoint {
	ts: number;
	pkg_w: number;
	core_w: number;
	uncore_w: number;
	dram_w: number;
}
export interface CStatePoint {
	ts: number;
	[state: string]: number;
}
export interface DiskPoint {
	ts: number;
	active: number;
	reads_d: number;
	writes_d: number;
	power_state: PowerState;
}
export interface OverheadPoint {
	ts: number;
	avg_watts: number;
	gbp_year: number;
	db_bytes: number;
	samples: number;
	cpu_ms_day: number;
}
export interface ConfigView {
	interval_s: number;
	budget_gbp_year: number;
	data_dir: string;
	tunnel_hostname: string;
	mode: Mode;
	disks: {
		dev: string;
		role: string;
		label: string;
		mount: string;
		rotational: boolean;
		protected: boolean;
		monitored: boolean;
		pool: string;
	}[];
}

// ---- endpoints -------------------------------------------------------------
export const api = {
	health: () => req<{ ok: boolean; version: string; ts: number; mode: Mode }>('/api/health'),
	status: () => req<Status>('/api/status'),
	diskEvents: (since = 0, limit = 200, dev = '') =>
		req<{ events: DiskEvent[] }>(
			`/api/disks/events?since=${since}&limit=${limit}${dev ? `&dev=${dev}` : ''}`
		),
	powerSeries: (from: number, to: number, res: 'raw' | 'hour' = 'raw') =>
		req<{ points: PowerPoint[] }>(`/api/timeseries/power?from=${from}&to=${to}&res=${res}`),
	cstateSeries: (from: number, to: number, scope: 'package' | 'core', res: 'raw' | 'hour' = 'raw') =>
		req<{ scope: string; states: string[]; points: CStatePoint[] }>(
			`/api/timeseries/cstate?from=${from}&to=${to}&scope=${scope}&res=${res}`
		),
	diskSeries: (dev: string, from: number, to: number, res: 'raw' | 'hour' = 'raw') =>
		req<{ dev: string; points: DiskPoint[] }>(
			`/api/timeseries/disk?dev=${dev}&from=${from}&to=${to}&res=${res}`
		),
	processes: () => req<{ procs: ProcInfo[] }>('/api/processes'),
	powerEvents: (since = 0, limit = 200) =>
		req<{ events: PowerEvent[] }>(`/api/power/events?since=${since}&limit=${limit}`),
	overhead: (from: number, to: number) =>
		req<{ points: OverheadPoint[]; current: OverheadPoint; budget_gbp: number }>(
			`/api/overhead?from=${from}&to=${to}`
		),
	config: () => req<ConfigView>('/api/config'),
	// diagnostic (explicit, gated)
	diagStart: (body: { tool: string; seconds: number; dev?: string; confirm_wake?: boolean }) =>
		req<{ session_id: string }>('/api/diag/start', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body)
		}),
	diagStatus: () =>
		req<{ running: boolean; sessions: { id: string; tool: string; started: number; ends: number; dev?: string }[] }>(
			'/api/diag/status'
		),
	diagResult: (id: string) =>
		req<{ id: string; tool: string; lines: string[]; summary: string; started: number; ended: number }>(
			`/api/diag/result/${id}`
		)
};
