<script lang="ts">
	interface Props {
		value: number;
		onChange: (seconds: number) => void;
	}
	let { value, onChange }: Props = $props();

	const presets: Array<{ label: string; seconds: number }> = [
		{ label: '1h', seconds: 3600 },
		{ label: '6h', seconds: 21600 },
		{ label: '24h', seconds: 86400 },
		{ label: '7d', seconds: 604800 }
	];
</script>

<div class="range" role="group" aria-label="Time range">
	{#each presets as p (p.seconds)}
		<button
			type="button"
			class="btn"
			class:active={value === p.seconds}
			aria-pressed={value === p.seconds}
			onclick={() => onChange(p.seconds)}
		>
			{p.label}
		</button>
	{/each}
</div>

<style>
	.range {
		display: inline-flex;
		gap: 4px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 3px;
	}
	.btn {
		border: none;
		background: transparent;
		color: var(--text-secondary);
		padding: 4px 12px;
		border-radius: var(--radius-sm);
		font-weight: 500;
		line-height: 1.2;
	}
	.btn:hover {
		color: var(--text-primary);
	}
	.btn.active {
		background: var(--surface-1);
		color: var(--series-1);
		font-weight: 700;
	}
</style>
