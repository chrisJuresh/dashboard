<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, type ConfigView } from '$lib/api';
	import { poll } from '$lib/stores';
	import { fmtRelative, fmtClock, fmtDateTime } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import Badge from '$lib/components/Badge.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	// ---- local shapes mirroring the diag endpoints -------------------------
	type Session = { id: string; tool: string; started: number; ends: number; dev?: string };
	type DiagResult = {
		id: string;
		tool: string;
		lines: string[];
		summary: string;
		started: number;
		ended: number;
	};

	type ToolMeta = {
		id: string;
		label: string;
		desc: string;
		/** requires a target rotational device */
		dev: boolean;
		/** can spin up / wake a standby disk */
		wakes: boolean;
	};

	const TOOLS: ToolMeta[] = [
		{ id: 'audit', label: 'wake audit (auditd)', desc: 'Which processes open the raw disk devices (SMART/smartctl/dd) or browse/scan the mount roots — the invisible wakes block-I/O tracing misses. No device needed. (For deep per-file reads, use ext4slower/biosnoop.)', dev: false, wakes: false },
		{ id: 'biosnoop', label: 'biosnoop', desc: 'Trace block I/O with the issuing process (eBPF). System-wide.', dev: false, wakes: false },
		{ id: 'ext4slower', label: 'ext4slower', desc: 'Slow ext4 operations attributed to a process (eBPF).', dev: false, wakes: false },
		{ id: 'bpftrace_bio', label: 'bpftrace bio', desc: 'Block-I/O latency and counts via a bpftrace one-liner.', dev: false, wakes: false },
		{ id: 'blktrace', label: 'blktrace', desc: 'Detailed block-layer trace for one device.', dev: true, wakes: false },
		{ id: 'turbostat', label: 'turbostat', desc: 'Per-core C-state residency and package power (reads MSRs).', dev: false, wakes: false },
		{ id: 'powertop', label: 'powertop', desc: 'System-wide snapshot of top power consumers.', dev: false, wakes: false },
		{ id: 'smart', label: 'smart (smartctl)', desc: 'Read SMART attributes for one device — CAN spin up a standby disk.', dev: true, wakes: true }
	];

	// ---- reactive state -----------------------------------------------------
	let config = $state<ConfigView | null>(null);
	let configError = $state('');

	let unreachable = $state(false);
	let unreachableMsg = $state('');

	// controls
	let tool = $state('biosnoop');
	let seconds = $state(15);
	let dev = $state('');
	let understand = $state(false); // "I understand this may spin up the disk"

	// run lifecycle
	let starting = $state(false);
	let startError = $state('');
	let activeSessionId = $state<string | null>(null);
	let monitoring = $state(false); // fast (2s) poll while a run is live

	// status + result
	let running = $state(false);
	let sessions = $state<Session[]>([]);
	let result = $state<DiagResult | null>(null);
	let resultError = $state('');

	let preEl = $state<HTMLPreElement | null>(null);

	// ---- derived ------------------------------------------------------------
	const meta = $derived(TOOLS.find((t) => t.id === tool) ?? TOOLS[0]);
	const rotDisks = $derived(config ? config.disks.filter((d) => d.rotational) : []);
	const needsDev = $derived(meta.dev);
	const activeSession = $derived(
		activeSessionId ? (sessions.find((s) => s.id === activeSessionId) ?? null) : null
	);

	// ---- helpers ------------------------------------------------------------
	function isConnError(e: unknown): boolean {
		return e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable');
	}
	function errMsg(e: unknown): string {
		if (e instanceof ApiError) return e.message || `Request failed (${e.status}).`;
		return e instanceof Error ? e.message : 'Unknown error.';
	}
	function handleConnError(e: unknown): boolean {
		if (!isConnError(e)) return false;
		unreachable = true;
		unreachableMsg =
			e instanceof ApiError && e.message === 'not-configured'
				? 'No agent connection is configured yet. Add the API base URL + token on the connect screen.'
				: 'The a3watch agent is not reachable. This is expected when viewing on Vercel without the cloudflared tunnel up.';
		return true;
	}
	function secs(n: number): string {
		return `${Math.max(0, Math.round(n))}s`;
	}
	function toolLabel(id: string): string {
		return TOOLS.find((t) => t.id === id)?.label ?? id;
	}

	async function loadConfig() {
		try {
			const c = await api.config();
			config = c;
			unreachable = false;
			configError = '';
			if (!dev) {
				const rot = c.disks.filter((d) => d.rotational);
				if (rot.length) dev = rot[0].dev;
			}
		} catch (e) {
			if (!handleConnError(e)) configError = errMsg(e);
		}
	}

	async function viewSession(id: string) {
		resultError = '';
		activeSessionId = id;
		try {
			result = await api.diagResult(id);
			unreachable = false;
		} catch (e) {
			if (!handleConnError(e)) resultError = errMsg(e);
		}
	}

	async function start() {
		startError = '';
		if (needsDev && !dev) {
			startError = 'Select a target device for this tool.';
			return;
		}
		if (tool === 'smart' && !understand) {
			startError = 'You must confirm you understand this may spin up the disk.';
			return;
		}
		const s = Math.max(1, Math.min(300, Math.round(Number(seconds) || 0)));
		seconds = s;

		const body: { tool: string; seconds: number; dev?: string; confirm_wake?: boolean } = {
			tool,
			seconds: s
		};
		if (needsDev) body.dev = dev;
		if (tool === 'smart') body.confirm_wake = understand; // gate wake behind explicit confirm

		starting = true;
		try {
			const { session_id } = await api.diagStart(body);
			activeSessionId = session_id;
			result = null;
			resultError = '';
			running = true;
			monitoring = true; // switch the poll effect into fast (2s) mode
			unreachable = false;
		} catch (e) {
			// DiagError comes back as { error: msg } → ApiError.message
			if (!handleConnError(e)) startError = errMsg(e);
		} finally {
			starting = false;
		}
	}

	// ---- polling ------------------------------------------------------------
	// Always poll diag/status so the recent-sessions list + running flag stay
	// fresh (20s, the live cadence). While a run is monitored, drop to 2s and
	// also fetch the result so the captured lines stream in.
	$effect(() => {
		const fast = monitoring;
		const sid = activeSessionId;
		return poll(
			async () => {
				const st = await api.diagStatus();
				let res: DiagResult | null = null;
				let resErr = '';
				if (fast && sid) {
					try {
						res = await api.diagResult(sid);
					} catch (e) {
						if (isConnError(e)) throw e;
						resErr = errMsg(e); // result not ready yet, etc. — non-fatal
					}
				}
				return { st, res, resErr };
			},
			fast ? 2000 : 20000,
			({ st, res, resErr }) => {
				unreachable = false;
				running = st.running;
				sessions = st.sessions;
				if (res) {
					result = res;
					resultError = '';
				} else if (resErr) {
					resultError = resErr;
				}
				if (!config) void loadConfig();
				// run finished: capture the final result (fetched this tick) then downshift
				if (fast && !st.running) monitoring = false;
			},
			(e) => {
				handleConnError(e);
			}
		);
	});

	// keep the live output scrolled to the tail while a run streams
	$effect(() => {
		void result?.lines.length;
		if (preEl && running) preEl.scrollTop = preEl.scrollHeight;
	});

	onMount(() => {
		void loadConfig();
	});
</script>

<PageHeader
	title="Diagnostic mode"
	subtitle="explicit, time-boxed, higher overhead — off by default"
/>

{#if unreachable}
	<EmptyState title="Agent not reachable" message={unreachableMsg} />
{:else}
	<div class="stack">
		<!-- Safety warning -->
		<Card>
			<div class="warn">
				<div class="warn-head">
					<Badge status="warning" label="Read this first" icon="!" />
				</div>
				<ul class="warn-list">
					<li>
						<span class="lead good-ink">Normal monitoring never wakes a disk</span> — it only reads
						collected samples and non-waking counters.
					</li>
					<li>
						<span class="lead warn-ink">Diagnostic tracers add overhead</span> while they run
						(extra CPU, eBPF/MSR reads). They are time-boxed and stop on their own.
					</li>
					<li>
						<span class="lead serious-ink">The <code>smart</code> tool can spin up a standby disk.</span>
						It is gated behind an extra explicit confirmation below.
					</li>
				</ul>
				{#if config}
					<p class="mode muted">
						Agent is currently in <strong class="tabular">{config.mode}</strong> mode.
					</p>
				{/if}
			</div>
		</Card>

		<!-- Controls -->
		<Card title="Start a diagnostic">
			<div class="controls">
				<label class="field">
					<span class="flabel">Tool</span>
					<select bind:value={tool}>
						{#each TOOLS as t (t.id)}
							<option value={t.id}>{t.label}</option>
						{/each}
					</select>
				</label>

				<label class="field">
					<span class="flabel">Duration (seconds)</span>
					<input
						type="number"
						min="1"
						max="300"
						step="1"
						class="tabular"
						bind:value={seconds}
					/>
				</label>

				<label class="field">
					<span class="flabel">
						Device {#if needsDev}<span class="req">required</span>{:else}<span class="muted">n/a</span>{/if}
					</span>
					<select bind:value={dev} disabled={!needsDev || rotDisks.length === 0}>
						{#if rotDisks.length === 0}
							<option value="">no rotational disks</option>
						{:else}
							{#each rotDisks as d (d.dev)}
								<option value={d.dev}>{d.label || d.dev} ({d.dev})</option>
							{/each}
						{/if}
					</select>
				</label>
			</div>

			<p class="desc muted">{meta.desc}</p>

			{#if meta.wakes}
				<label class="confirm">
					<input type="checkbox" bind:checked={understand} />
					<span>
						<Badge status="serious" label="Wake risk" icon="⏻" />
						I understand this may spin up the disk.
					</span>
				</label>
			{/if}

			{#if needsDev && rotDisks.length === 0}
				<p class="err serious-ink">This tool needs a rotational device, but none are configured.</p>
			{/if}
			{#if configError}
				<p class="err serious-ink">Could not load device list: {configError}</p>
			{/if}
			{#if startError}
				<p class="err critical-ink">{startError}</p>
			{/if}

			<div class="actions">
				<button class="start" onclick={start} disabled={starting || running}>
					{#if starting}Starting…{:else if running}Session running…{:else}Start{/if}
				</button>
				{#if running}
					<Badge status="warning" label="a session is running" icon="●" />
				{/if}
			</div>
		</Card>

		<!-- Live / selected output -->
		{#if activeSessionId}
			<Card title="Capture">
				<div class="cap-head">
					<div class="cap-meta">
						<span class="cap-tool tabular">{toolLabel(activeSession?.tool ?? result?.tool ?? tool)}</span>
						{#if activeSession?.dev}<span class="muted tabular"> · {activeSession.dev}</span>{/if}
						{#if activeSessionId}<span class="muted tabular sid"> · {activeSessionId}</span>{/if}
					</div>
					{#if running && monitoring}
						<Badge status="warning" label="running" icon="●" />
					{:else}
						<Badge status="good" label="done" icon="✓" />
					{/if}
				</div>

				{#if running && monitoring && activeSession}
					<p class="countdown muted tabular">
						Time-boxed to {secs(activeSession.ends - activeSession.started)}, ends at
						{fmtClock(activeSession.ends)}.
					</p>
				{/if}

				{#if result?.summary}
					<p class="summary">{result.summary}</p>
				{/if}

				{#if resultError}
					<p class="err serious-ink">{resultError}</p>
				{/if}

				{#if result && result.lines.length > 0}
					<pre bind:this={preEl} class="output tabular">{result.lines.join('\n')}</pre>
				{:else if running}
					<pre class="output muted">Waiting for output…</pre>
				{:else if result}
					<pre class="output muted">No output captured.</pre>
				{/if}
			</Card>
		{/if}

		<!-- Recent sessions -->
		<Card title="Recent sessions">
			{#if sessions.length === 0}
				<p class="muted">No diagnostic sessions yet.</p>
			{:else}
				<div class="table-wrap">
					<table class="sessions">
						<thead>
							<tr>
								<th>Tool</th>
								<th>Device</th>
								<th class="num">Duration</th>
								<th>Started</th>
								<th class="num"></th>
							</tr>
						</thead>
						<tbody>
							{#each sessions as s (s.id)}
								<tr class:sel={s.id === activeSessionId}>
									<td class="tabular">{toolLabel(s.tool)}</td>
									<td class="tabular muted">{s.dev ?? '—'}</td>
									<td class="num tabular">{secs(s.ends - s.started)}</td>
									<td class="muted" title={fmtDateTime(s.started)}>{fmtRelative(s.started)}</td>
									<td class="num">
										<button class="view" onclick={() => viewSession(s.id)}>View</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</Card>
	</div>
{/if}

<style>
	.stack {
		display: flex;
		flex-direction: column;
		gap: var(--gap);
	}

	/* warning card */
	.warn-head {
		margin-bottom: 10px;
	}
	.warn-list {
		margin: 0;
		padding-left: 18px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		color: var(--text-secondary);
		font-size: 13px;
		line-height: 1.5;
	}
	.lead {
		font-weight: 650;
	}
	.good-ink {
		color: var(--good);
	}
	.warn-ink {
		color: var(--warning);
	}
	.serious-ink {
		color: var(--serious);
	}
	.critical-ink {
		color: var(--critical);
	}
	code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		background: var(--surface-2);
		border-radius: var(--radius-sm);
		padding: 0 4px;
	}
	.mode {
		margin: 12px 0 0;
		font-size: 12px;
	}

	/* controls */
	.controls {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 0;
	}
	.flabel {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.req {
		color: var(--warning);
		font-size: 11px;
		font-weight: 600;
	}
	select,
	input[type='number'] {
		font-family: inherit;
		font-size: 14px;
		color: var(--text-primary);
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		width: 100%;
	}
	select:disabled {
		color: var(--text-muted);
		cursor: not-allowed;
	}
	.desc {
		margin: 12px 0 0;
		font-size: 13px;
	}
	.confirm {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		margin-top: 12px;
		font-size: 13px;
		color: var(--text-secondary);
		line-height: 1.4;
	}
	.confirm input {
		margin-top: 2px;
	}
	.confirm span {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.err {
		margin: 10px 0 0;
		font-size: 13px;
		font-weight: 500;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-top: 14px;
	}
	.start {
		background: var(--series-1);
		color: white;
		border: none;
		border-radius: var(--radius-sm);
		padding: 9px 20px;
		font-weight: 650;
	}
	.start:disabled {
		background: var(--surface-2);
		color: var(--text-muted);
		cursor: not-allowed;
	}

	/* capture */
	.cap-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
	}
	.cap-tool {
		font-weight: 650;
	}
	.sid {
		font-size: 12px;
	}
	.countdown {
		margin: 8px 0 0;
		font-size: 12px;
	}
	.summary {
		margin: 10px 0 0;
		color: var(--text-secondary);
		font-size: 13px;
	}
	.output {
		margin: 12px 0 0;
		max-height: 380px;
		overflow: auto;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 12px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 12px;
		line-height: 1.45;
		white-space: pre;
		color: var(--text-primary);
	}

	/* sessions table */
	.table-wrap {
		overflow-x: auto;
	}
	.sessions {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}
	.sessions th,
	.sessions td {
		text-align: left;
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}
	.sessions th {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
		font-weight: 600;
	}
	.sessions .num {
		text-align: right;
	}
	.sessions tr.sel {
		background: var(--surface-2);
	}
	.view {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text-secondary);
		border-radius: var(--radius-sm);
		padding: 4px 12px;
		font-size: 12px;
		font-weight: 600;
	}
	.view:hover {
		color: var(--text-primary);
		border-color: var(--axis);
	}
</style>
