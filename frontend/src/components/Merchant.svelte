<script>
	import {
		Bell, BookOpenText, HandCoins, KeyRound, Lightbulb, MessageCircle, ShieldCheck, Tag, Users, Zap,
	} from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';
	import { app } from '../lib/state.svelte.js';
	import Chat from './Chat.svelte';
	import Timeline from './Timeline.svelte';

	const hints = [
		[HandCoins, 'Money has not reached me', 'Mera paisa nahi aaya'],
		[BookOpenText, 'Collect Khata dues', 'Udhaar vasool karna hai'],
		[Tag, 'Bring in new customers', 'Naye customer ke liye offer'],
	];
	const tiers = {
		T1: 'Soundbox only. Saathi speaks and asks as little as possible.',
		T2: 'Billing and payments: Khata, payment links, invoices.',
		T3: 'Operations and stock. The fullest view.',
	};
	const plain = {
		retry_settlement: 'Resend a failed settlement',
		raise_device_ticket: 'Report a Soundbox fault',
		create_payment_link: 'Make a payment link',
		notify: 'Send you an update',
		request_bank_detail_update: 'Change your bank details',
		send_khata_reminder: 'Remind customers about dues',
		create_offer_campaign: 'Launch an offer',
		start_loan_application: 'Start a loan application',
	};
	const groups = [
		['L2', 'Does on its own, then tells you', Zap, 'chip-go'],
		['L3', 'Asks you first: one tap, or say “haan”', ShieldCheck, 'chip-wait'],
		['L1', 'Only suggests', Lightbulb, ''],
	];
	const tabs = [
		['chat', 'Chat', MessageCircle],
		['customers', 'Customers', Users],
		['rules', 'Permissions', KeyRound],
		['inbox', 'Updates', Bell],
	];
	let tab = $state('chat');
	let d = $state(null);
	const me = $derived(app.boot.merchants.find((m) => m.id === app.mid));

	async function load(id = app.mid) {
		const fresh = await api(`/merchant/${id}`);
		if (id === app.mid) d = fresh;
	}
	$effect(() => {
		load(app.mid);
	});
</script>

<div class="grid items-start justify-center gap-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,32rem)]">
	<div class="phone">
		<div class="bg-face px-4 py-4 text-white">
			<div class="flex items-center gap-3">
				<span class="grid size-11 shrink-0 place-items-center rounded-2xl bg-white/20 font-display text-lg font-extrabold">
					{me.tier}
				</span>
				<div class="min-w-0 flex-1">
					<select
						bind:value={app.mid}
						aria-label="Choose a demo shop"
						class="w-full truncate rounded-lg bg-transparent font-display text-lg font-bold"
					>
						{#each app.boot.merchants as m}
							<option class="text-ink" value={m.id}>{m.name} ({m.tier})</option>
						{/each}
					</select>
					<p class="text-xs text-white/80">{tiers[me.tier]}</p>
				</div>
			</div>
		</div>

		<div class="flex min-h-0 flex-1 flex-col overflow-auto">
			{#if tab === 'chat'}
				{#key app.mid}
					<Chat type="merchant" id={app.mid} lang={me.language} {hints} onchange={load} />
				{/key}
			{:else if !d}
				<p class="hint p-5">Loading…</p>
			{:else if tab === 'customers'}
				<div class="grid gap-6 p-5">
					<section class="grid gap-3">
						<h2 class="title">Customers near you</h2>
						{#if !d.cohorts.length}
							<p class="hint">No group near you is large enough to show yet.</p>
						{/if}
						{#each d.cohorts as c}
							<div class="flex items-center gap-3 rounded-2xl bg-face-soft p-3">
								<span class="font-display text-3xl font-extrabold text-face tabular-nums">{c.size}</span>
								<p class="text-sm">people nearby {c.description.replace(/^\d+ nearby users /, '')}</p>
							</div>
						{/each}
						<p class="hint">
							These are counts of people who chose to get offers. You never see names or numbers,
							and groups under {app.boot.limits.COHORT_MIN_SIZE} are hidden.
						</p>
					</section>

					<section class="grid gap-3">
						<h2 class="title">Your offers</h2>
						{#if !d.offers.length}
							<p class="hint">No offers yet. Ask in chat: “Naye customer ke liye offer”.</p>
						{/if}
						{#each d.offers as o}
							<article class="rounded-2xl border border-line p-4">
								<h3 class="font-semibold">{o.title}</h3>
								<dl class="ledger mt-1">
									<div><dt>Sent to</dt><dd>{o.delivered}</dd></div>
									<div><dt>Used</dt><dd>{o.redeemed}</dd></div>
									<div><dt>Held back to compare</dt><dd>{o.holdout}</dd></div>
									<div>
										<dt>Extra sales from the offer</dt>
										<dd class="text-go">{inr(o.incremental_inr)}</dd>
									</div>
									<div><dt>Discount you paid</dt><dd>{inr(o.discount_cost)}</dd></div>
								</dl>
							</article>
						{/each}
					</section>

					<p class="flex items-center gap-2 text-sm">
						<span class="icon-dot"><BookOpenText size={18} /></span>
						<span><b class="tabular-nums">{d.khata_open}</b> Khata entries are still open.</span>
					</p>
				</div>
			{:else if tab === 'rules'}
				<div class="grid gap-4 p-5">
					<div>
						<h2 class="title">What Saathi may do for you</h2>
						<p class="hint mt-1">This changes with your plan. Switch shops above to compare.</p>
					</div>
					{#each groups as [level, label, Icon, tone]}
						{@const list = Object.entries(d.autonomy).filter(([t, l]) => l === level && plain[t])}
						{#if list.length}
							<section class="rounded-2xl border border-line p-4">
								<h3 class="flex items-center gap-2 text-sm font-semibold">
									<span class="chip {tone}"><Icon size={13} /></span>
									{label}
								</h3>
								<ul class="mt-2 grid gap-1 text-sm">
									{#each list as [t]}<li>{plain[t]}</li>{/each}
								</ul>
							</section>
						{/if}
					{/each}
					<p class="hint">
						Anything risky, disputed or above {inr(app.boot.limits.ESCALATE_ABOVE_INR)} goes to a
						person.
					</p>
				</div>
			{:else}
				<div class="grid gap-3 p-5">
					<h2 class="title">Updates</h2>
					{#if !d.inbox.length}
						<div class="grid justify-items-center gap-2 py-8 text-center">
							<span class="icon-dot size-12"><Bell size={22} /></span>
							<p class="hint">No updates yet.</p>
						</div>
					{/if}
					{#each d.inbox as n (n.id)}
						<p class="flex items-start gap-3 rounded-2xl border border-line p-3 text-sm">
							<span class="icon-dot size-8 rounded-xl"><Bell size={15} /></span>
							{n.message}
						</p>
					{/each}
				</div>
			{/if}
		</div>

		<div class="tabbar" role="tablist">
			{#each tabs as [k, label, Icon]}
				<button role="tab" aria-selected={tab === k} onclick={() => ((tab = k), load())}>
					<Icon size={19} />
					{label}
				</button>
			{/each}
		</div>
	</div>

	<Timeline />
</div>
