<script>
	import { PiggyBank, Target, TrendingUp } from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';

	let { userId } = $props();
	let d = $state(null);
	let note = $state('');
	const top = $derived(Math.max(...(d?.summary.by_category.map((c) => c.total) ?? [1]), 1));
	const peak = $derived(Math.max(...(d?.trend.months.map((m) => m.total) ?? [1]), 1));

	async function load() {
		d = await api(`/consumer/${userId}/spend`);
	}
	$effect(() => {
		userId;
		load();
	});

	async function recategorise(t, category) {
		note = '';
		try {
			const r = await api(`/consumer/${userId}/category`, 'POST', {
				merchant_name: t.merchant,
				category,
			});
			note = `${r.merchant} is now ${r.category}. I will remember that.`;
			load();
		} catch (e) {
			note = e.message;
		}
	}
</script>

{#if !d}
	<p class="hint p-5">Loading…</p>
{:else if !d.enabled}
	<div class="grid justify-items-center gap-2 p-8 text-center">
		<span class="icon-dot size-12"><TrendingUp size={22} /></span>
		<p class="hint max-w-60">Spend insights are switched off. Turn them on under My data.</p>
	</div>
{:else}
	<div class="grid gap-6 p-5">
		<section class="grid gap-3">
			<div>
				<h2 class="title">Spent this month</h2>
				<p class="font-display text-4xl font-extrabold tabular-nums">{inr(d.summary.total)}</p>
				<p class="hint">
					At this pace, about {inr(d.forecast.projected)} by month end{d.forecast.budget
						? d.forecast.overshoot
							? `, ${inr(d.forecast.overshoot)} over your budget`
							: ', inside your budget'
						: ''}.
				</p>
			</div>
			{#each d.summary.by_category as c}
				<div class="grid gap-1 text-sm">
					<div class="flex justify-between">
						<span class="capitalize">{c.category}</span>
						<b class="tabular-nums">{inr(c.total)}</b>
					</div>
					<div class="h-2.5 rounded-full bg-ground">
						<div class="h-full rounded-full bg-face" style:width="{(100 * c.total) / top}%"></div>
					</div>
				</div>
			{/each}
		</section>

		<section class="grid gap-2">
			<h3 class="font-semibold">Month by month</h3>
			<div class="flex h-28 items-end gap-2">
				{#each d.trend.months as m, i}
					<div class="flex flex-1 flex-col items-center gap-1">
						<span class="text-[0.65rem] text-mute tabular-nums">{inr(m.total)}</span>
						<div
							class="w-full rounded-t-lg {i === d.trend.months.length - 1 ? 'bg-face' : 'bg-face-soft'}"
							style:height="{Math.max((70 * m.total) / peak, 3)}px"
						></div>
						<span class="text-[0.65rem] text-mute">{m.month.slice(0, 3)}</span>
					</div>
				{/each}
			</div>
		</section>

		<section class="grid gap-3">
			<h3 class="flex items-center gap-2 font-semibold"><PiggyBank size={17} /> Budgets</h3>
			{#if !d.budgets.budgets.length}
				<p class="hint">No budgets yet. Ask in chat: “plan a budget for me”.</p>
			{/if}
			{#each d.budgets.budgets as b}
				<div class="grid gap-1 text-sm">
					<div class="flex justify-between">
						<span class="capitalize">{b.category}</span>
						<span class="tabular-nums">
							{inr(b.spent)} of {inr(b.limit)}
							{#if b.alert}<span class="chip chip-stop ml-1">{b.pct}% used</span>{/if}
						</span>
					</div>
					<div class="h-2.5 overflow-hidden rounded-full bg-ground">
						<div
							class="h-full rounded-full {b.pct >= 100 ? 'bg-stop' : b.alert ? 'bg-sun' : 'bg-go'}"
							style:width="{Math.min(b.pct, 100)}%"
						></div>
					</div>
				</div>
			{/each}
			{#each d.goals as g}
				<p class="flex items-center gap-2 rounded-2xl bg-face-soft p-3 text-sm text-face-deep">
					<Target size={16} class="shrink-0" />
					{g.name}: {inr(g.target)} in {g.months} months, {inr(Math.ceil(g.target / g.months))} a month
				</p>
			{/each}
		</section>

		<section class="grid gap-2">
			<h3 class="font-semibold">Recent payments</h3>
			<p class="hint text-xs">Wrong category? Change it and Saathi remembers for that shop.</p>
			{#each d.recent as t}
				<div class="flex items-center gap-2 rounded-2xl bg-ground px-3 py-2 text-sm">
					<span class="min-w-0 flex-1">
						<span class="block truncate font-semibold">{t.merchant}</span>
						<span class="text-xs text-mute">{t.date}</span>
					</span>
					<select
						class="field py-1 text-xs capitalize"
						aria-label="Category for {t.merchant}"
						value={t.category}
						onchange={(e) => recategorise(t, e.currentTarget.value)}
					>
						{#each d.categories as c}<option value={c}>{c}</option>{/each}
					</select>
					<b class="w-16 text-right tabular-nums">{inr(t.amount)}</b>
				</div>
			{/each}
			{#if note}<p class="rounded-2xl bg-go-soft px-4 py-2 text-sm text-go">{note}</p>{/if}
		</section>
		<p class="hint text-xs">Information about your own spending, not financial advice. Never shared with shops.</p>
	</div>
{/if}
