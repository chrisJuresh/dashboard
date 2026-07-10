/*
 * a3watch API client. The dashboard is served by the agent itself, behind
 * Cloudflare Access, so every call is a same-origin relative fetch — Cloudflare
 * handles login (its session cookie rides along automatically) and the agent
 * verifies the Access token. No API URL or bearer token in the browser.
 * Every call is read-only except the explicit /diag/* endpoints.
 */

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
	// Relative, same-origin: the agent served this page; the Cloudflare Access
	// cookie is sent automatically and the edge injects the verified login token.
	let resp: Response;
	try {
		resp = await fetch(path, { credentials: 'same-origin', ...init });
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
		model?: string;
		serial?: string;
		auto_detected?: boolean;
	}[];
}

// ---- system map (docker / tunnel / git / systemd / domains / ~/) -----------
export interface TreeNode {
	name: string;
	type: 'dir' | 'file' | 'symlink' | 'mount';
	size?: number;
	files?: number;
	mtime?: number;
	collapsed?: boolean;
	truncated?: boolean;
	note?: string;
	children?: TreeNode[];
}
export interface SysMap {
	home: string;
	git: {
		path: string;
		remote: string;
		branch: string;
		upstream: string;
		last_commit: string;
		dirty_files: number;
		ci_workflows: string[];
	}[];
	docker: {
		containers: {
			name: string;
			image: string;
			state: string;
			restarts: number;
			restart_policy: string;
			networks: string[];
			ports: string[];
			mounts: string[];
			cmd: string;
		}[];
		networks: { name: string; driver: string }[];
		compose_files: string[];
	};
	tunnel: { running: boolean; container: string; image: string; note: string };
	access_domains: {
		access: { enabled: boolean; team_domain: string; aud: string };
		hostnames: string[];
		cors_allow_origins: string[];
		api_bind: string;
	};
	systemd: {
		units: {
			unit: string;
			description: string;
			active: string;
			enabled: string;
			schedule?: string;
			exec?: string;
		}[];
	};
	secrets: {
		path: string;
		kind: string;
		mode: string;
		owner: string;
		world_accessible: boolean;
		note: string;
	}[];
	home_tree: {
		root?: TreeNode;
		total_files?: number;
		total_bytes?: number;
		nodes_shown?: number;
		capped?: boolean;
		error?: string;
	};
	pipelines: { name: string; steps: string[] }[];
	cloud_snapshot: unknown | null;
}

// ---- metrics (generic collector output) ------------------------------------
export interface Metric {
	collector: string;
	key: string;
	num: number | null;
	txt: string | null;
	unit: string;
	ts: number;
}
export interface MetricGroup {
	group: string;
	metrics: Metric[];
}
export interface MetricsLatest {
	ts: number;
	groups: MetricGroup[];
}
export interface MetricSeriesPoint {
	ts: number;
	num: number;
}
export interface MetricSeries {
	key: string;
	points: MetricSeriesPoint[];
}

// ---- endpoints -------------------------------------------------------------
export const api = {
	health: () => req<{ ok: boolean; version: string; ts: number; mode: Mode }>('/api/health'),
	// who is signed in (via Cloudflare Access); informational only
	session: () => req<{ authenticated: boolean; email: string | null }>('/api/session'),
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
	// full read-only map of the box (docker / tunnel / git / systemd / domains / ~/)
	sysmap: () => req<SysMap>('/api/sysmap'),
	// generic metrics feed (latest snapshot grouped for display)
	metricsLatest: () => req<MetricsLatest>('/api/metrics/latest'),
	metricSeries: (key: string, from: number, to: number, res: 'raw' | 'hour' = 'raw') =>
		req<MetricSeries>(
			`/api/metrics/series?key=${encodeURIComponent(key)}&from=${from}&to=${to}&res=${res}`
		),
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
