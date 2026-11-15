<script>
	import { Check, CircleAlert, ScanText, ShieldCheck } from '@lucide/svelte';
	import { api } from '../lib/api.js';

	let { userId, review, onresult = () => {} } = $props();
	let values = $state(Object.fromEntries(review.fields.map((f) => [f.name, f.value])));
	let ticks = $state({});
	let ack = $state(false);
	let busy = $state(false);
	let done = $state(!['clean', 'review', 'unreadable'].includes(review.state));
	let note = $state('');
	const anomaly = $derived(review.flags?.anomaly);
	const tone = { ok: 'chip-go', confirm: 'chip-wait', low: 'chip-stop' };

	async function confirm() {
		busy = true;
		note = '';
		try {
			const r = await api(`/consumer/${userId}/bills/${review.upload_id}/confirm`, 'POST', {
				values,
				confirmed: Object.keys(ticks).filter((k) => ticks[k]),
				acknowledge_anomaly: ack,
			});
			if (r.approval || r.state === 'duplicate' || r.state === 'error') {
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
		<span class="icon-dot"><ScanText size={18} /></span>
		<h3 class="font-display text-base font-bold">What I read from your bill</h3>
	</header>

	<div class="grid gap-2">
		{#each review.fields as f}
			{@const needsTick = f.state !== 'ok' && ['biller', 'consumer_number', 'amount_due', 'due_date'].includes(f.name)}
			<div class="grid gap-1 rounded-xl p-2 {f.state === 'ok' ? '' : 'bg-sun-soft/60'}">
				<div class="flex items-center justify-between gap-2">
					<label for="f{review.upload_id}{f.name}" class="text-xs font-semibold text-mute">{f.label}</label>
					<span class="chip {tone[f.state]}" title="How sure Saathi is, worked out from the text">
						{Math.round(f.confidence * 100)}% sure
					</span>
				</div>
				<input
					id="f{review.upload_id}{f.name}"
					class="field"
					bind:value={values[f.name]}
					disabled={done}
					placeholder={f.name === 'consumer_number' ? f.display : ''}
				/>
				{#if f.issue}<p class="text-xs text-sun-deep">{f.issue}</p>{/if}
				{#if needsTick && !done}
					<label class="flex items-center gap-1.5 text-xs">
						<input type="checkbox" bind:checked={ticks[f.name]} class="accent-face" />
						I have checked this against my bill
					</label>
				{/if}
			</div>
		{/each}
	</div>

	{#if anomaly && !done}
		<label class="flex items-start gap-2 rounded-xl bg-stop-soft p-3 text-stop">
			<input type="checkbox" bind:checked={ack} class="mt-0.5 accent-stop" />
			<span>
				<b>₹{anomaly.amount} is about {anomaly.times} times your usual ₹{anomaly.average}.</b>
				Tick to confirm this amount is right.
			</span>
		</label>
	{/if}

	<p class="flex gap-1.5 text-xs text-mute">
		<ShieldCheck size={14} class="mt-0.5 shrink-0" />
		{review.notice}
	</p>
	{#if note}
		<p class="flex gap-1.5 rounded-xl bg-sun-soft p-2 text-xs text-sun-deep">
			<CircleAlert size={14} class="mt-0.5 shrink-0" />
			{note}
		</p>
	{/if}
	{#if !done}
		<button class="btn btn-solid" disabled={busy} onclick={confirm}>
			<Check size={16} /> These details are right
		</button>
	{/if}
</article>
