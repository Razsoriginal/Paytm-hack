<script>
	import { Boxes, CircleAlert, PackageX } from '@lucide/svelte';
	import { api, inr } from '../lib/api.js';

	let { merchantId, bump = 0 } = $props();
	let d = $state(null);

	$effect(() => {
		bump;
		api(`/merchant/${merchantId}/stock`).then((r) => (d = r));
	});
</script>

{#if !d}
	<p class="hint p-5">Loading…</p>
{:else}
	<div class="grid gap-5 p-5">
		<div>
			<h2 class="title">Your stock</h2>
			<p class="hint mt-1">
				{d.can_edit
					? 'Change stock in chat, by voice, or by uploading a supplier invoice with the paperclip.'
					: 'On the Soundbox plan you can ask about stock by voice. Changing stock needs the billing plan.'}
			</p>
		</div>

		{#if d.low.products.length}
			<section class="grid gap-2 rounded-2xl bg-sun-soft p-3">
				<h3 class="flex items-center gap-2 text-sm font-semibold text-sun-deep">
					<CircleAlert size={16} /> Running low
				</h3>
				{#each d.low.products as p}
					<div class="flex items-center justify-between gap-2 rounded-xl bg-white px-3 py-2 text-sm">
						<span>
							<b>{p.name}</b>
							<span class="block text-xs text-mute">
								{p.stock} left{p.days_left != null ? `, about ${p.days_left} days` : ''}
							</span>
						</span>
						<span class="chip chip-wait">Reorder {p.reorder_qty}</span>
					</div>
				{/each}
				<p class="text-xs text-sun-deep">{d.low.assumption}.</p>
			</section>
		{/if}

		<section class="grid gap-1.5">
			<h3 class="flex items-center gap-2 font-semibold"><Boxes size={17} /> All products</h3>
			{#each d.products as p}
				<div class="flex items-center gap-3 rounded-2xl bg-ground px-3 py-2 text-sm">
					<span class="min-w-0 flex-1">
						<span class="block truncate font-semibold">{p.name}</span>
						<span class="text-xs text-mute">
							₹{p.cost} cost, ₹{p.price} price, {p.margin_pct}% margin
						</span>
					</span>
					<span class="text-right leading-tight">
						<b class="tabular-nums {p.low ? 'text-stop' : ''}">{p.stock}</b>
						<span class="block text-xs text-mute">
							{p.days_left != null ? `${p.days_left} days` : 'no sales'}
						</span>
					</span>
				</div>
			{/each}
		</section>

		{#if d.dead.length}
			<section class="grid gap-2">
				<h3 class="flex items-center gap-2 font-semibold"><PackageX size={17} /> Not selling</h3>
				{#each d.dead as p}
					<p class="flex justify-between rounded-2xl border border-line px-3 py-2 text-sm">
						<span>{p.name}, {p.stock} in stock</span>
						<b class="tabular-nums">{inr(p.value)} tied up</b>
					</p>
				{/each}
			</section>
		{/if}
	</div>
{/if}
