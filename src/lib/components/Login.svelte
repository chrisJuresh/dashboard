<script lang="ts">
	import { api, ApiError } from '$lib/api';

	let { onLoggedIn }: { onLoggedIn?: () => void } = $props();

	let email = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit(e: Event) {
		e.preventDefault();
		if (busy) return;
		busy = true;
		error = '';
		try {
			await api.login(email.trim(), password);
			onLoggedIn?.();
		} catch (err) {
			if (err instanceof ApiError && err.status === 429) {
				error = 'Too many attempts — wait a few minutes.';
			} else if (err instanceof ApiError && err.status === 401) {
				error = 'Invalid email or password.';
			} else {
				error = 'Could not reach the agent.';
			}
		} finally {
			busy = false;
			password = '';
		}
	}
</script>

<div class="wrap">
	<form class="card" onsubmit={submit}>
		<h1>a3watch</h1>
		<p class="secondary">Sign in to view your server stats.</p>
		<label>
			<span>Email</span>
			<input type="email" bind:value={email} autocomplete="username" required placeholder="you@example.com" />
		</label>
		<label>
			<span>Password</span>
			<input type="password" bind:value={password} autocomplete="current-password" required />
		</label>
		{#if error}<p class="error">{error}</p>{/if}
		<button class="submit" type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
	</form>
</div>

<style>
	.wrap {
		min-height: 100vh;
		display: grid;
		place-items: center;
		padding: 24px;
	}
	.card {
		width: 100%;
		max-width: 360px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	h1 {
		margin: 0;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 6px;
		font-size: 13px;
		color: var(--text-secondary);
	}
	input {
		padding: 10px 12px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-2);
		color: var(--text-primary);
		font: inherit;
	}
	input:focus {
		outline: none;
		border-color: var(--series-1);
	}
	.error {
		color: var(--critical);
		font-size: 13px;
		margin: 0;
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
