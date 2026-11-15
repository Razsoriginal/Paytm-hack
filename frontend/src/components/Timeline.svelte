<script>
	import {
		BadgeCheck, Ban, Check, Eye, Radio, Scale, TrendingUp, UserRoundCheck, X, Zap,
	} from '@lucide/svelte';
	import { app } from '../lib/state.svelte.js';

	let { limit = 12, title = 'What Saathi is doing' } = $props();
	const stages = {
		detect: ['Noticed', Eye],
		decide: ['Decided', Scale],
		act: ['Acted', Zap],
		approve: ['Approval', BadgeCheck],
		measure: ['Measured', TrendingUp],
		escalate: ['Handed to a person', UserRoundCheck],
	};
	const levels = { L2: 'on its own', L3: 'with approval' };
	const words = (s) => s.replaceAll('_', ' ');

	/** "pay_from_wallet ✓ [L3: approved]" → readable parts. */
	function parse(r) {
		const act = r.detail.match(/^(\w+) ([✓✗]) ?(?:\[(L\d): ([\w:]+)\])?(.*)$/);
		if (act) {
			const [, tool, mark, level, reason, rest] = act;
			return {
				text: words(tool),
				ok: mark === '✓',
				note: mark === '✓' ? (levels[level] ?? '') : words(rest.trim()),
				why: reason === 'saga_compensation' ? 'undo step' : '',
			};
		}
		const ruled = r.detail.match(/^(\w+) → (\w+) \(([\w:,]+)\)$/);
		if (ruled) {
			const [, tool, outcome, reason] = ruled;
			return { text: words(tool), ok: false, blocked: true, note: `${words(outcome)}: ${words(reason)}` };
		}
		return { text: r.detail, ok: null };
	}

	// Identical steps in a row (15 offers delivered) collapse into one line with a count.
	const shown = $derived.by(() => {
		const out = [];
		for (const r of app.runs) {
			const last = out.at(-1);
			if (last && last.agent === r.agent && last.detail === r.detail) {
				last.count += 1;
				last.id = r.id;
			} else {
				out.push({ ...r, count: 1, ...parse(r) });
			}
		}
		return out.slice(-limit).reverse();
	});

	function tone(r) {
		if (r.ok === false || r.stage === 'escalate') return 'bg-stop-soft text-stop';
		if (r.stage === 'approve') return 'bg-sun-soft text-sun-deep';
		return r.ok ? 'bg-go-soft text-go' : 'bg-face-soft text-face';
	}
</script>

<section class="card grid gap-4">
	<div class="flex items-start justify-between gap-3">
		<div>
			<h2 class="title">{title}</h2>
			<p class="hint mt-1">
				Fixed rules check every step before it runs. The language model only suggests.
			</p>
		</div>
		<span class="chip chip-go"><Radio size={12} /> Live</span>
	</div>

	{#if !shown.length}
		<div class="grid justify-items-center gap-2 py-10 text-center">
			<span class="icon-dot size-12"><Zap size={22} /></span>
			<p class="hint max-w-64">Send a message and each step will appear here as it happens.</p>
		</div>
	{/if}

	<ol class="grid gap-1">
		{#each shown as r (r.id)}
			{@const [label, Icon] = stages[r.stage] ?? [r.stage, Zap]}
			<li class="flex gap-3 rounded-2xl p-2 {r.id > app.fresh ? 'arrive' : ''}">
				<span class="grid size-9 shrink-0 place-items-center rounded-full {tone(r)}">
					{#if r.blocked}
						<Ban size={17} />
					{:else if r.ok === false}
						<X size={17} />
					{:else if r.ok}
						<Check size={17} />
					{:else}
						<Icon size={17} />
					{/if}
				</span>
				<div class="min-w-0 text-sm leading-snug">
					<p class="text-xs font-semibold text-mute">
						{r.agent === label ? label : `${label} by ${r.agent}`}
					</p>
					<p class="[overflow-wrap:anywhere] {r.ok === null ? '' : 'font-semibold first-letter:uppercase'}">
						{r.text}
					</p>
					{#if r.note || r.why || r.count > 1}
						<p class="mt-1 flex flex-wrap gap-1">
							{#if r.note}<span class="chip {r.ok === false ? 'chip-stop' : ''}">{r.note}</span>{/if}
							{#if r.why}<span class="chip chip-wait">{r.why}</span>{/if}
							{#if r.count > 1}<span class="chip tabular-nums">{r.count} times</span>{/if}
						</p>
					{/if}
				</div>
			</li>
		{/each}
	</ol>
</section>
