<script lang="ts">
	import { setConnection, api, getApiBase, getToken, ApiError } from '$lib/api';

	let { onConnected }: { onConnected?: () => void } = $props();

	let base = $state(getApiBase());
	let token = $state(getToken());
	let busy = $state(false);
	let error = $state('');

	function friendly(err: unknown): string {
		if (err instanceof ApiError) {
			if (err.message === 'unreachable')
				return 'Could not reach the agent. Check the URL and that the cloudflared tunnel is up.';
			if (err.status === 401 || err.status === 403) return 'Unauthorized — check the bearer token.';
			return err.message || `Request failed (${err.status}).`;
		}
		return err instanceof Error ? err.message : 'Connection failed.';
	}

	async function connect(e: Event) {
		e.preventDefault();
		error = '';
		const b = base.trim();
		if (!b) {
			error = 'Enter the API base URL.';
			return;
		}
		busy = true;
		setConnection(b, token.trim());
		try {
			await api.health();
			onConnected?.();
		} catch (err) {
			error = friendly(err);
		} finally {
			busy = false;
		}
	}
</script>

<div class="wrap">
	<form class="card panel" onsubmit={connect}>
		<div class="head">
			<h1>Connect to a3watch</h1>
			<p class="muted lede">
				Point the dashboard at your a3watch agent. Nothing is sent anywhere else — the URL and token
				are stored only in this browser, and all data stays on your server.
			</p>
		</div>

		<label class="field">
			<span class="label">API base URL</span>
			<input
				type="url"
				bind:value={base}
				placeholder="https://a3watch.example.com"
				autocomplete="off"
				spellcheck="false"
				disabled={busy}
			/>
		</label>

		<label class="field">
			<span class="label">Bearer token</span>
			<input
				type="password"
				bind:value={token}
				placeholder="paste the agent token"
				autocomplete="off"
				spellcheck="false"
				disabled={busy}
			/>
		</label>

		{#if error}
			<p class="error" role="alert">{error}</p>
		{/if}

		<button class="submit" type="submit" disabled={busy}>
			{busy ? 'Connecting…' : 'Connect'}
		</button>
	</form>
</div>

<style>
	.wrap {
		display: flex;
		align-items: center;
		justify-content: center;
		min-height: 100vh;
		padding: 24px;
	}
	.panel {
		width: 100%;
		max-width: 420px;
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 24px;
	}
	.head h1 {
		font-size: 20px;
	}
	.lede {
		margin: 8px 0 0;
		font-size: 13px;
		line-height: 1.5;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.label {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
	}
	input {
		font-family: inherit;
		font-size: 14px;
		padding: 9px 11px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-2);
		color: var(--text-primary);
	}
	input:focus {
		outline: none;
		border-color: var(--series-1);
	}
	input:disabled {
		opacity: 0.6;
	}
	.error {
		margin: 0;
		font-size: 13px;
		color: var(--critical);
	}
	.submit {
		padding: 10px 14px;
		border-radius: var(--radius-sm);
		border: 1px solid transparent;
		background: var(--series-1);
		color: var(--on-accent);
		font-weight: 650;
	}
	.submit:hover:not(:disabled) {
		filter: brightness(1.08);
	}
	.submit:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
