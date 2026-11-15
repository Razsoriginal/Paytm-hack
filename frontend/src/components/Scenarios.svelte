<script>
	import {
		BookOpenText, Boxes, CalendarClock, ChartPie, CircleAlert, Gift, HandCoins, LoaderCircle, Plane,
		Play, ReceiptText, RotateCcw,
	} from '@lucide/svelte';
	import { api } from '../lib/api.js';
	import { app, resetAll } from '../lib/state.svelte.js';
	import Timeline from './Timeline.svelte';

	const groups = {
		C1: ['Book a trip', Plane],
		C2: ['Fix a failed payment or refund', CircleAlert],
		C3: ['Upload a bill and pay', ReceiptText],
		C4: ['Change or cancel a booking', CalendarClock],
		C5: ['Ask about spending', ChartPie],
		B1: ['Turn preferences into an offer', Gift],
		W1: ['Money has not reached the shop', HandCoins],
		W2: ['Collect Khata dues', BookOpenText],
		M1: ['Manage stock', Boxes],
	};
	let runs = $state([]);
	let busy = $state('');
	const byGroup = $derived(
		Object.keys(groups).map((g) => [g, app.boot.scenarios.filter((s) => s.startsWith(g))]),
	);
	const short = (name) => name.slice(3).replace(/^./, (c) => c.toUpperCase());

	function pretty(text) {
		if (!text.startsWith('{')) return text;
		const o = JSON.parse(text);
		return Object.entries(o)
			.filter(([k, v]) => v !== null && k !== 'note' && k !== 'title')
			.map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`)
			.join('\n');
	}

	async function run(name) {
		busy = name;
		try {
			runs.unshift(await api(`/scenarios/${encodeURIComponent(name)}`, 'POST'));
		} catch (e) {
			runs.unshift({ scenario: name, steps: [{ who: 'error', said: '', saathi: e.message }] });
		}
		busy = '';
	}

	async function reset() {
		busy = 'reset';
		await resetAll();
		runs = [];
		busy = '';
	}
</script>

<div class="grid items-start gap-6 lg:grid-cols-2">
	<section class="card grid gap-5">
		<div>
			<h2 class="title">Run a planted situation</h2>
			<p class="hint mt-1">
				Each one sends real messages through Saathi and approves on the user's behalf. Sold-out and
				price-change cases fire once, so start over to repeat them.
			</p>
		</div>
		{#each byGroup as [g, list]}
			{@const [label, Icon] = groups[g]}
			<div class="grid gap-2">
				<h3 class="flex items-center gap-2 font-semibold">
					<span class="icon-dot size-8 rounded-xl"><Icon size={16} /></span>
					{label}
				</h3>
				<div class="flex flex-wrap gap-2">
					{#each list as name}
						<button class="btn" disabled={!!busy} onclick={() => run(name)}>
							{#if busy === name}
								<LoaderCircle size={14} class="animate-spin" />
							{:else}
								<Play size={14} />
							{/if}
							{short(name)}
						</button>
					{/each}
				</div>
			</div>
		{/each}
		<div>
			<button class="btn btn-danger" disabled={!!busy} onclick={reset}>
				<RotateCcw size={15} /> Start over with fresh data
			</button>
		</div>
	</section>

	<div class="grid gap-6">
		{#each runs as r}
			<section class="card grid gap-2">
				<h3 class="font-display text-lg font-bold">{short(r.scenario)}</h3>
				{#each r.steps as s}
					{#if s.said}
						<p class="max-w-[85%] justify-self-end rounded-3xl rounded-br-md bg-face px-4 py-2 text-sm text-white">
							{s.said}
						</p>
					{/if}
					<p class="max-w-[92%] rounded-3xl rounded-bl-md bg-ground px-4 py-2 text-sm whitespace-pre-wrap">
						{pretty(s.saathi)}
					</p>
				{/each}
			</section>
		{/each}
		<Timeline limit={8} />
	</div>
</div>
