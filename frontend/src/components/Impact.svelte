<script>
	import { Gift, Plug, RefreshCw, ScrollText, ShieldCheck, Store, User } from '@lucide/svelte';
	import { api, inr, pct } from '../lib/api.js';
	import { app, loadPartners } from '../lib/state.svelte.js';
	import Timeline from './Timeline.svelte';

	let m = $state(null);
	let audit = $state([]);
	let face = $state('consumer');
	const b = $derived(m?.bridge);
	const funnel = $derived(
		b
			? [
					['People who opted in and match', b.matched],
					['Got the offer', b.delivered],
					['Used it', b.redeemed],
				]
			: [],
	);
	const partnerNames = {
		sarvam_llm: 'Sarvam language model',
		sarvam_speech: 'Sarvam voice and translation',
		cognee: 'Cognee memory',
		paytm_pg: 'Paytm payment gateway',
	};
	const outcomeTone = { success: 'chip-go', failed: 'chip-stop', blocked: 'chip-stop' };

	async function load() {
		[m, audit] = await Promise.all([api('/impact'), api('/audit?limit=40')]);
	}
	$effect(() => {
		app.runs.length;
		load();
	});
</script>

{#if !m}
	<p class="hint">Adding up the numbers…</p>
{:else}
	<div class="grid items-start gap-6 lg:grid-cols-2">
		<section class="card grid gap-4">
			<div class="flex items-center gap-3">
				<span class="icon-dot"><Gift size={18} /></span>
				<div>
					<h2 class="title">From preference to purchase</h2>
					{#if b}<p class="hint">{b.title}</p>{/if}
				</div>
			</div>
			{#if !b}
				<p class="hint">No offer has run yet. Open Scenarios and run “Offer bridge”.</p>
			{:else}
				<div class="grid gap-2">
					{#each funnel as [label, n], i}
						<div class="flex items-center gap-3">
							<div
								class="flex h-11 min-w-14 items-center rounded-2xl px-4 font-display text-xl font-extrabold tabular-nums {[
									'bg-face-soft text-face-deep',
									'bg-face/70 text-white',
									'bg-face text-white',
								][i]}"
								style:width="{Math.max(100 * (n / (b.matched || 1)), 14)}%"
							>
								{n}
							</div>
							<span class="text-sm whitespace-nowrap text-mute">{label}</span>
						</div>
					{/each}
				</div>
				<div class="rounded-2xl bg-go-soft p-4">
					<p class="text-sm font-semibold text-go">Extra sales the offer caused</p>
					<p class="font-display text-4xl font-extrabold text-go tabular-nums">
						{inr(b.incremental_inr)}
					</p>
				</div>
				<dl class="ledger">
					<div><dt>Used the offer</dt><dd>{pct(b.redemption_rate * 100)}</dd></div>
					<div>
						<dt>Bought anyway with no offer ({b.holdout} held back)</dt>
						<dd>{pct(b.holdout_rate * 100)}</dd>
					</div>
					<div><dt>Discount the shop paid</dt><dd>{inr(b.discount_cost)}</dd></div>
					<div>
						<dt>Switched offers off afterwards</dt>
						<dd>{pct(b.opt_out_rate * 100)}</dd>
					</div>
				</dl>
				<p class="hint">
					Simulated responses and small groups. This shows how uplift is measured, not a firm result.
				</p>
			{/if}
		</section>

		<section class="card grid gap-4">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<h2 class="title">Outcomes</h2>
				<div class="flex gap-1 rounded-full bg-ground p-1" role="tablist">
					{#each [['consumer', 'Consumers', User], ['merchant', 'Merchants', Store]] as [k, label, Icon]}
						<button
							role="tab"
							aria-selected={face === k}
							onclick={() => (face = k)}
							class="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-semibold text-mute aria-selected:bg-white aria-selected:text-face aria-selected:shadow-sm"
						>
							<Icon size={15} />
							{label}
						</button>
					{/each}
				</div>
			</div>

			{#if face === 'consumer'}
				<div class="rounded-2xl bg-go-soft p-4">
					<p class="text-sm font-semibold text-go">Money returned to users</p>
					<p class="font-display text-4xl font-extrabold text-go tabular-nums">
						{inr(m.consumer.refunded_inr)}
					</p>
				</div>
				<dl class="ledger">
					<div><dt>Trips booked</dt><dd>{m.consumer.trips_booked}</dd></div>
					<div><dt>Value of trips</dt><dd>{inr(m.consumer.trip_value_inr)}</dd></div>
					<div><dt>Typical time to book</dt><dd>{m.consumer.time_to_book_p50_s ?? '–'} s</dd></div>
					<div><dt>Failed checkouts undone cleanly</dt><dd>{m.consumer.saga_recoveries}</dd></div>
					<div>
						<dt>Price changes caught before charging</dt>
						<dd>{m.consumer.hash_mismatches}</dd>
					</div>
					<div>
						<dt>Payment problems fixed without a person</dt>
						<dd>{pct(m.consumer.auto_resolution_pct)}</dd>
					</div>
				</dl>
			{:else}
				<div class="rounded-2xl bg-go-soft p-4">
					<p class="text-sm font-semibold text-go">Dues recovered after reminders</p>
					<p class="font-display text-4xl font-extrabold text-go tabular-nums">
						{inr(m.merchant.khata_recovered_inr)}
					</p>
				</div>
				<dl class="ledger">
					<div><dt>Recovered with no reminder</dt><dd>{inr(m.merchant.khata_holdout_inr)}</dd></div>
					<div><dt>Reminders sent</dt><dd>{m.merchant.reminders_sent}</dd></div>
					<div>
						<dt>Service problems fixed without a person</dt>
						<dd>{m.merchant.auto_resolved}</dd>
					</div>
				</dl>
			{/if}

			<div class="grid grid-cols-3 gap-2 text-center">
				{#each [['Approvals given', pct(m.approval_rate_pct), ''], ['Handed to a person', pct(m.escalation_pct), ''], ['Rule violations', m.policy_violations.length, m.policy_violations.length ? 'text-stop' : 'text-go']] as [label, value, tone]}
					<div class="rounded-2xl bg-ground p-3">
						<p class="font-display text-2xl font-extrabold tabular-nums {tone}">{value}</p>
						<p class="text-xs text-mute">{label}</p>
					</div>
				{/each}
			</div>
		</section>

		<Timeline limit={10} title="Every step, live" />

		<section class="card grid gap-3">
			<div class="flex items-center gap-3">
				<span class="icon-dot"><ScrollText size={18} /></span>
				<div>
					<h2 class="title">Audit log</h2>
					<p class="hint">Append-only. Entries cannot be edited or removed.</p>
				</div>
			</div>
			<div class="grid max-h-[34rem] gap-1.5 overflow-auto pr-1">
				{#each audit as a (a.id)}
					<div class="rounded-2xl bg-ground px-3 py-2 text-sm [overflow-wrap:anywhere]">
						<p class="flex flex-wrap items-center gap-1.5">
							<span class="chip {outcomeTone[a.outcome] ?? ''}">{a.outcome || 'note'}</span>
							<b>{a.what.replaceAll('_', ' ').replace(':', ' · ')}</b>
						</p>
						<p class="text-xs text-mute">
							{a.actor}{a.why ? `, ${a.why}` : ''}{a.original_text ? ` “${a.original_text}”` : ''}
						</p>
					</div>
				{/each}
			</div>
		</section>

		<section class="card grid gap-3 lg:col-span-2">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div class="flex items-center gap-3">
					<span class="icon-dot"><Plug size={18} /></span>
					<h2 class="title">Connected services</h2>
				</div>
				<button class="btn" onclick={loadPartners}><RefreshCw size={15} /> Test connections</button>
			</div>
			<div class="grid gap-2 sm:grid-cols-2">
				{#each Object.entries(app.partners ?? {}) as [k, p]}
					<div class="flex items-center gap-3 rounded-2xl border border-line p-3">
						<div class="min-w-0 flex-1 text-sm">
							<p class="font-semibold">{partnerNames[k]}</p>
							<p class="text-xs text-mute">{p.detail}</p>
						</div>
						<span class="chip {p.mode === 'live' ? 'chip-go' : 'chip-wait'}">
							{#if p.mode === 'live'}<ShieldCheck size={12} />{/if}
							{p.mode === 'live' ? 'Live' : 'Mock'}
						</span>
					</div>
				{/each}
			</div>
		</section>
	</div>
{/if}
