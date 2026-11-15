<script>
	import { Check, PackagePlus, ShieldCheck } from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';

	let { merchantId, review, onresult = () => {} } = $props();
	let items = $state(review.items.map((i) => ({ ...i })));
	let busy = $state(false);
	let done = $state(false);
	let note = $state('');
	const total = $derived(items.reduce((sum, i) => sum + Number(i.qty) * Number(i.rate), 0));

	async function confirm() {
		busy = true;
		try {
			const r = await api(`/merchant/${merchantId}/stock-upload/${review.upload_id}/confirm`, 'POST', {
				items,
			});
			if (r.approval) {
				done = true;
				onresult(r);
			} else {
				note = r.reply;
			}
		} catch (e) {
			note = e.message;
		}
		busy = false;
	}
</script>

<article class="grid gap-3 rounded-2xl border border-line bg-white p-4 text-sm">
	<header class="flex items-center gap-2.5">
		<span class="icon-dot"><PackagePlus size={18} /></span>
		<h3 class="font-display text-base font-bold">Items I read. Check every line.</h3>
	</header>
	<div class="grid gap-2">
		{#each items as it, i}
			<div class="grid gap-1.5 rounded-xl p-2 {it.state === 'ok' ? 'bg-ground' : 'bg-sun-soft/70'}">
				<div class="flex items-center gap-2">
					<input class="field flex-1" bind:value={it.name} disabled={done} aria-label="Item {i + 1} name" />
					<span class="chip {it.state === 'ok' ? 'chip-go' : 'chip-wait'}">
						{Math.round(it.confidence * 100)}% sure
					</span>
				</div>
				<div class="flex items-center gap-2 text-xs text-mute">
					<label>Qty <input type="number" class="field ml-1 w-20" bind:value={it.qty} disabled={done} /></label>
					<label>Rate ₹ <input type="number" class="field ml-1 w-20" bind:value={it.rate} disabled={done} /></label>
					{#if it.new}<span class="chip">New product</span>{/if}
				</div>
			</div>
		{/each}
	</div>
	<p class="flex items-baseline justify-between border-t-2 border-ink pt-2 font-display font-bold">
		Total <span class="text-lg tabular-nums">{inr(total)}</span>
	</p>
	<p class="flex gap-1.5 text-xs text-mute">
		<ShieldCheck size={14} class="mt-0.5 shrink-0" />
		{review.notice}
	</p>
	{#if note}<p class="rounded-xl bg-sun-soft p-2 text-xs text-sun-deep">{note}</p>{/if}
	{#if !done}
		<button class="btn btn-solid" disabled={busy} onclick={confirm}>
			<Check size={16} /> These lines are right
		</button>
	{/if}
</article>
