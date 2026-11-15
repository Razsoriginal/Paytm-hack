<script>
	import {
		BedDouble, Bell, BusFront, CalendarClock, ChartPie, CircleAlert, Gift, Info, LifeBuoy, Luggage,
		MessageCircle, Plane, Plus, ReceiptText, ShieldCheck, TrainFront, Trash, Wallet, X,
	} from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';
	import { app } from '../lib/state.svelte.js';
	import Chat from './Chat.svelte';
	import Receipt from './Receipt.svelte';
	import Spend from './Spend.svelte';
	import Timeline from './Timeline.svelte';

	const categories = ['budget', 'travel_mode', 'cuisine', 'pace', 'price_sensitivity', 'time_slot'];
	const hints = [
		[Plane, 'Plan and book a trip', 'Mumbai se Goa kal 2 raat'],
		[CircleAlert, 'Fix a failed payment', 'Mere paise kat gaye'],
		[ChartPie, 'Understand my spending', 'How much did I spend on food last month?'],
	];
	const attach = $derived({
		purpose: 'bill',
		path: `/consumer/${app.uid}/bills`,
		label: 'Upload a bill to pay',
	});
	let dates = $state({});
	let changing = $state({});
	const kindIcon = { train: TrainFront, bus: BusFront, flight: Plane, hotel: BedDouble };
	const consentTitle = {
		personalisation: 'Personal plans',
		offers: 'Offers from nearby shops',
		spend_insights: 'Spend insights',
	};
	let tab = $state('chat');
	let d = $state(null);
	let cat = $state('budget');
	let val = $state('');
	let bills = $state({});
	let redeeming = $state({});
	let note = $state('');
	const me = $derived(app.boot.users.find((u) => u.id === app.uid));
	const offers = $derived(d?.inbox.filter((n) => n.meta.type === 'offer').length ?? 0);
	const tabs = $derived([
		['chat', 'Chat', MessageCircle],
		['trips', 'Trips', Luggage],
		['spend', 'Spend', ChartPie],
		['inbox', offers ? `Inbox (${offers})` : 'Inbox', Bell],
		['data', 'My data', ShieldCheck],
	]);

	async function load(id = app.uid) {
		const fresh = await api(`/consumer/${id}`);
		if (id === app.uid) d = fresh;
	}
	$effect(() => {
		load(app.uid);
	});

	async function act(fn) {
		note = '';
		try {
			await fn();
		} catch (e) {
			note = e.message;
		}
		load();
	}
	const setConsent = (scope, granted) =>
		act(() => api(`/consumer/${app.uid}/consent`, 'POST', { scope, granted }));
	const savePref = () =>
		act(async () => {
			await api(`/consumer/${app.uid}/prefs`, 'POST', { category: cat, value: val });
			val = '';
		});
	const change = (b, action) =>
		act(async () => {
			const text = action === 'concern' ? window.prompt('What went wrong with this trip?') : '';
			if (action === 'concern' && !text) return;
			const r = await api(`/consumer/${app.uid}/bookings/${b.id}/change`, 'POST', {
				action,
				new_date: dates[b.id],
				text,
			});
			note = r.reply;
			if (r.approval) changing[b.id] = r.approval;
		});
	const startRedeem = (n) =>
		act(async () => {
			const bill = Number(bills[n.id] ?? Math.ceil(n.meta.min_bill * 1.2));
			const r = await api(`/consumer/${app.uid}/redeem`, 'POST', {
				offer_id: n.meta.offer_id,
				bill_amount: bill,
			});
			redeeming[n.id] = r.approval;
		});
</script>

<div class="grid items-start justify-center gap-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,32rem)]">
	<div class="phone">
		<div class="flex items-center gap-3 bg-face px-4 py-4 text-white">
			<span class="grid size-11 shrink-0 place-items-center rounded-full bg-white/20 font-display text-lg font-bold">
				{me.name[0]}
			</span>
			<div class="min-w-0 flex-1">
				<select
					bind:value={app.uid}
					aria-label="Choose a demo user"
					class="w-full truncate rounded-lg bg-transparent font-display text-lg font-bold"
				>
					{#each app.boot.users as u}
						<option class="text-ink" value={u.id}>{u.name}</option>
					{/each}
				</select>
				<p class="text-xs text-white/80">{me.city}</p>
			</div>
			<div class="rounded-2xl bg-white/15 px-3 py-1.5 text-right leading-tight">
				<p class="flex items-center justify-end gap-1 text-[0.7rem] text-white/80">
					<Wallet size={12} /> Wallet
				</p>
				<p class="font-display text-lg font-extrabold tabular-nums">{inr(d?.wallet)}</p>
			</div>
		</div>

		<div class="flex min-h-0 flex-1 flex-col overflow-auto">
			{#if tab === 'chat'}
				{#key app.uid}
					<Chat
						type="consumer"
						id={app.uid}
						lang={me.language}
						name={me.name.split(' ')[0]}
						{hints}
						{attach}
						onchange={load}
					/>
				{/key}
			{:else if !d}
				<p class="hint p-5">Loading…</p>
			{:else if tab === 'trips'}
				<div class="grid gap-4 p-5">
					<h2 class="title">Your trips</h2>
					{#if !d.bookings.length}
						<div class="grid justify-items-center gap-2 py-8 text-center">
							<span class="icon-dot size-12"><Luggage size={22} /></span>
							<p class="hint max-w-60">No trips yet. Ask in chat: “Mumbai se Goa kal 2 raat”.</p>
						</div>
					{/if}
					{#each d.bookings as b (b.id)}
						{@const live = b.status === 'confirmed'}
						{@const upcoming = b.travel_date >= new Date().toISOString().slice(0, 10)}
						<article class="grid gap-3 rounded-2xl border border-line p-4">
							<div class="flex items-start justify-between gap-3">
								<h3 class="font-semibold">{b.title}</h3>
								<p class="font-display font-extrabold tabular-nums">{inr(b.total)}</p>
							</div>
							<ol class="grid gap-2">
								{#each b.items as it}
									{@const Icon = kindIcon[it.kind] ?? Luggage}
									<li class="flex items-center gap-2.5 text-sm">
										<span class="icon-dot size-8 rounded-xl"><Icon size={16} /></span>
										<span class="flex-1">{it.details.name}</span>
										<span class="chip {it.status === 'confirmed' ? 'chip-go' : 'chip-stop'}">{it.status}</span>
									</li>
								{/each}
							</ol>
							{#if b.timeline.length > 1}
								<ol class="grid gap-1 border-l-2 border-line pl-3 text-xs text-mute">
									{#each b.timeline as ev}<li>{ev.event}</li>{/each}
								</ol>
							{/if}
							{#if changing[b.id]}
								<Receipt
									approval={changing[b.id]}
									onresult={(r) => {
										note = r.reply;
										changing[b.id] = r.approval ?? null;
										load();
									}}
								/>
							{:else if live}
								<div class="grid gap-2 border-t border-line pt-3">
									{#if upcoming}
									<div class="flex flex-wrap items-center gap-2">
										<input
											type="date"
											class="field flex-1"
											aria-label="New travel date"
											oninput={(e) => (dates[b.id] = e.currentTarget.value)}
										/>
										<button class="btn" disabled={!dates[b.id]} onclick={() => change(b, 'reschedule')}>
											<CalendarClock size={15} /> Reschedule
										</button>
									</div>
									{/if}
									<div class="flex flex-wrap gap-2">
										{#if upcoming}
											<button class="btn btn-danger" onclick={() => change(b, 'cancel')}>
												<X size={15} /> Cancel trip
											</button>
										{/if}
										<button class="btn" onclick={() => change(b, 'concern')}>
											<LifeBuoy size={15} /> Raise a concern
										</button>
									</div>
								</div>
							{/if}
						</article>
					{/each}
					{#if note}<p class="rounded-2xl bg-face-soft px-4 py-2 text-sm text-face-deep">{note}</p>{/if}
					{#if d.tickets.length}
						<h3 class="font-semibold">Your tickets</h3>
						{#each d.tickets as tk}
							<p class="flex items-center gap-2 rounded-2xl bg-ground px-3 py-2 text-sm">
								<ReceiptText size={15} class="shrink-0 text-face" />
								<span class="flex-1">Ticket {tk.ticket_id}: {tk.summary}</span>
								<span class="chip {tk.status === 'open' ? 'chip-wait' : 'chip-go'}">{tk.status}</span>
							</p>
						{/each}
					{/if}
				</div>
			{:else if tab === 'spend'}
				{#key app.uid}<Spend userId={app.uid} />{/key}
			{:else if tab === 'inbox'}
				<div class="grid gap-4 p-5">
					<h2 class="title">Inbox</h2>
					{#if !d.inbox.length}
						<div class="grid justify-items-center gap-2 py-8 text-center">
							<span class="icon-dot size-12"><Bell size={22} /></span>
							<p class="hint max-w-64">
								Nothing here yet. Offers from nearby shops arrive only if you switch them on under
								My data.
							</p>
						</div>
					{/if}
					{#each d.inbox as n (n.id)}
						{@const offer = n.meta.type === 'offer'}
						<article class="grid gap-3 rounded-2xl border border-line p-4">
							<div class="flex items-start gap-3">
								<span class="icon-dot">
									{#if offer}<Gift size={18} />{:else}<Bell size={18} />{/if}
								</span>
								<p class="text-sm font-semibold">{n.message}</p>
							</div>
							{#if offer}
								<details class="text-sm">
									<summary class="flex items-center gap-1 font-semibold text-face">
										<Info size={14} /> Why am I seeing this?
									</summary>
									<p class="hint mt-1">{n.meta.why}</p>
								</details>
								{#if redeeming[n.id]}
									<Receipt
										approval={redeeming[n.id]}
										onresult={(r) => {
											note = r.reply;
											load();
										}}
									/>
								{:else}
									<div class="flex flex-wrap items-center gap-2">
										<label class="hint" for="bill{n.id}">Bill amount</label>
										<input
											id="bill{n.id}"
											type="number"
											class="field w-24"
											value={Math.ceil(n.meta.min_bill * 1.2)}
											oninput={(e) => (bills[n.id] = e.currentTarget.value)}
										/>
										<button class="btn btn-solid" onclick={() => startRedeem(n)}>Use this offer</button>
									</div>
								{/if}
							{/if}
						</article>
					{/each}
					{#if note}<p class="rounded-2xl bg-sun-soft px-4 py-2 text-sm text-sun-deep">{note}</p>{/if}
				</div>
			{:else}
				<div class="grid gap-5 p-5">
					<h2 class="title">What Saathi knows about you</h2>

					<div class="grid gap-3">
						{#each Object.entries(d.consent_text) as [scope, words]}
							<label class="flex cursor-pointer items-start gap-3 rounded-2xl border border-line p-3">
								<input
									type="checkbox"
									class="peer sr-only"
									checked={d.consents[scope]}
									onchange={(e) => setConsent(scope, e.currentTarget.checked)}
								/>
								<span
									class="relative mt-0.5 h-6 w-11 shrink-0 rounded-full bg-line transition-colors peer-checked:bg-face peer-focus-visible:outline-3 peer-focus-visible:outline-sun after:absolute after:top-0.5 after:left-0.5 after:size-5 after:rounded-full after:bg-white after:shadow after:transition-transform peer-checked:after:translate-x-5"
								></span>
								<span class="text-sm">
									<span class="block font-semibold">{consentTitle[scope]}</span>
									<span class="text-mute">{words}</span>
								</span>
							</label>
						{/each}
					</div>

					<div class="grid gap-2">
						<h3 class="font-semibold">Your preferences</h3>
						{#if !d.preferences.length}
							<p class="hint">Nothing saved. Tell Saathi what you like, or add it below.</p>
						{/if}
						{#each d.preferences as p (p.id)}
							<div class="flex items-center gap-2 rounded-2xl bg-ground px-3 py-2 text-sm">
								<span class="flex-1">
									<span class="text-mute">{p.category.replace('_', ' ')}</span>
									<b class="ml-1">{p.value}</b>
								</span>
								{#if p.source === 'inferred'}
									<span class="chip chip-wait">We guessed this</span>
								{:else}
									<span class="chip">You told us</span>
								{/if}
								<button
									class="grid size-8 place-items-center rounded-full text-mute hover:bg-stop-soft hover:text-stop"
									aria-label="Delete {p.category} preference"
									onclick={() => act(() => api(`/consumer/${app.uid}/prefs/${p.id}`, 'DELETE'))}
								>
									<Trash size={15} />
								</button>
							</div>
						{/each}
						<form
							class="flex flex-wrap gap-2"
							onsubmit={(e) => {
								e.preventDefault();
								savePref();
							}}
						>
							<select bind:value={cat} aria-label="Preference type" class="field">
								{#each categories as c}<option value={c}>{c.replace('_', ' ')}</option>{/each}
							</select>
							<input
								bind:value={val}
								placeholder="for example: low"
								aria-label="Preference"
								class="field flex-[1_1_7rem]"
							/>
							<button class="btn" disabled={!val.trim()}><Plus size={15} /> Save</button>
						</form>
						{#if note}<p class="rounded-2xl bg-stop-soft px-4 py-2 text-sm text-stop">{note}</p>{/if}
					</div>

					<p class="flex gap-2 rounded-2xl bg-face-soft p-3 text-sm text-face-deep">
						<ShieldCheck size={18} class="mt-0.5 shrink-0" />
						Saathi never stores or guesses health, religion, caste or politics. Shops see only how
						many people are in a group, never who.
					</p>
					<button
						class="btn btn-danger"
						onclick={() => act(() => api(`/consumer/${app.uid}/prefs`, 'DELETE'))}
					>
						<Trash size={15} /> Delete everything Saathi knows
					</button>
				</div>
			{/if}
		</div>

		<div class="tabbar" role="tablist">
			{#each tabs as [k, label, Icon]}
				<button role="tab" aria-selected={tab === k} onclick={() => ((tab = k), (note = ''), load())}>
					<Icon size={19} />
					{label}
				</button>
			{/each}
		</div>
	</div>

	<Timeline />
</div>
