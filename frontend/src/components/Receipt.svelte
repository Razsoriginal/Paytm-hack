<script>
	import { BookOpenText, Check, Gift, Landmark, Plane, Smartphone, Tag, X } from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';

	let { approval, alt = null, onresult = () => {} } = $props();
	const icons = {
		trip: Plane,
		offer: Tag,
		khata: BookOpenText,
		redeem: Gift,
		bank_fix: Landmark,
		payment: Smartphone,
	};
	let busy = $state(false);
	let stamp = $state(approval.status === 'pending' ? '' : approval.status);
	const p = $derived(approval.payload);
	const Icon = $derived(icons[approval.kind] ?? Tag);
	const until = $derived(
		new Date(`${approval.expires_at}Z`).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
	);

	async function decide(yes) {
		busy = true;
		try {
			const res = await api(`/approvals/${approval.id}`, 'POST', { approve: yes });
			stamp = yes ? 'approved' : 'cancelled';
			onresult(res);
		} catch (e) {
			onresult({ reply: `That did not go through: ${e.message}` });
			busy = false;
		}
	}
</script>

{#snippet row(label, value, note = '')}
	<div class="flex items-baseline justify-between gap-3 border-b border-dashed border-face/25 py-1.5">
		<dt>{label}{#if note}<span class="ml-1.5 text-xs text-mute">{note}</span>{/if}</dt>
		<dd class="font-semibold whitespace-nowrap tabular-nums">{value}</dd>
	</div>
{/snippet}

{#snippet total(label, value)}
	<div class="flex items-baseline justify-between gap-3 border-t-2 border-ink pt-2 font-display">
		<dt class="font-bold">{label}</dt>
		<dd class="text-xl font-extrabold tabular-nums">{value}</dd>
	</div>
{/snippet}

<article class="receipt text-sm" aria-label={approval.title}>
	<header class="flex items-center gap-2.5">
		<span class="grid size-9 shrink-0 place-items-center rounded-xl bg-face text-white">
			<Icon size={18} />
		</span>
		<h3 class="font-display text-base leading-tight font-bold">{approval.title}</h3>
	</header>

	<dl>
		{#if approval.kind === 'trip'}
			{#each p.items as it}{@render row(it.name, inr(it.price), `× ${it.units}`)}{/each}
			{@render total('Total, charged once', inr(p.total))}
		{:else if approval.kind === 'offer'}
			{@render row('Discount', `${p.discount_pct}%`)}
			{@render row('Runs for', `${p.valid_days} days`)}
			{@render row('Redemptions expected', p.expected_redemptions)}
			{@render total('Most you will spend', inr(p.budget_cap))}
		{:else if approval.kind === 'khata'}
			<div class="max-h-44 overflow-auto pr-1">
				{#each p.entries as e}
					{@render row(e.customer, inr(e.amount), `${e.days_overdue} days late`)}
				{/each}
			</div>
			{@render total('Total to collect', inr(p.total))}
		{:else if approval.kind === 'redeem'}
			{@render row('Bill', inr(p.bill_amount))}
			{@render row('Offer saving', `−${inr(p.bill_amount - p.pay)}`)}
			{@render total('You pay', inr(p.pay))}
		{:else if approval.kind === 'bank_fix'}
			{@render row('Name on bank account', p.account_holder_name)}
			{@render total('Money to resend', inr(p.amount))}
		{:else if approval.kind === 'payment'}
			{@render total('From your wallet', inr(p.amount))}
		{/if}
	</dl>

	{#if approval.kind === 'trip'}
		<p class="text-xs text-mute">Why this plan: {p.why}.</p>
		{#if alt}
			<p class="text-xs text-mute">
				Other option: {alt.items.map((i) => i.name).join(', ')} for {inr(alt.total)}.
			</p>
		{/if}
	{:else if approval.kind === 'offer'}
		<p class="text-xs text-mute">{p.assumption}</p>
	{/if}

	{#if stamp}
		<div class="stamp {stamp === 'approved' ? '' : 'border-mute text-mute'}">
			{stamp === 'approved' ? 'Approved' : stamp === 'cancelled' ? 'Cancelled' : stamp}
		</div>
	{:else}
		<p class="text-xs text-mute">
			Your approval covers exactly this and nothing else. Valid until {until}.
		</p>
		<div class="flex gap-2">
			<button class="btn btn-solid flex-1" disabled={busy} onclick={() => decide(true)}>
				<Check size={16} /> Approve
			</button>
			<button class="btn" disabled={busy} onclick={() => decide(false)}>
				<X size={16} /> Cancel
			</button>
		</div>
	{/if}
</article>
