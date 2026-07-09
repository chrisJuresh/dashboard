<script lang="ts">
	type Status = 'good' | 'warning' | 'serious' | 'critical' | 'muted';

	let {
		label,
		value,
		sub,
		status
	}: { label: string; value: string; sub?: string; status?: Status } = $props();

	const colors: Record<Status, string> = {
		good: 'var(--good)',
		warning: 'var(--warning)',
		serious: 'var(--serious)',
		critical: 'var(--critical)',
		muted: 'var(--text-muted)'
	};

	const tint = $derived(status ? colors[status] : 'var(--text-primary)');
</script>

<div class="tile">
	<div class="label muted">{label}</div>
	<div class="value" style:color={tint}>
		{#if status}<span class="dot" style:background={colors[status]} aria-hidden="true"></span>{/if}<span
			>{value}</span
		>
	</div>
	{#if sub}<div class="sub muted">{sub}</div>{/if}
</div>

<style>
	.tile {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}
	.label {
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.02em;
	}
	.value {
		display: flex;
		align-items: baseline;
		gap: 8px;
		/* proportional (lining) figures for the hero number */
		font-variant-numeric: proportional-nums lining-nums;
		font-size: 28px;
		font-weight: 650;
		line-height: 1.1;
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		align-self: center;
		flex-shrink: 0;
	}
	.sub {
		font-size: 12px;
	}
</style>
