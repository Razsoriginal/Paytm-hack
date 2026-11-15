<script>
	import { Ban, Check, CircleCheck, Clock, Headset, Lightbulb, SendHorizontal, Store, User, X } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import { api, inr } from '../lib/api.js';

	let list = $state([]);
	let picked = $state(null);
	let reply = $state('');
	const cur = $derived(list.find((e) => e.id === picked) ?? list[0]);
	const open = $derived(list.filter((e) => e.status === 'open').length);
	const words = (r) => String(r ?? '').replace(/^risk:/, 'risk flag, ').replaceAll('_', ' ');

	async function load() {
		list = await api('/ops/escalations');
	}
	onMount(load);
	$effect(() => {
		reply = cur?.packet.suggested_reply ?? '';
	});

	async function resolve() {
		await api(`/ops/escalations/${cur.id}/resolve`, 'POST', { reply });
		load();
	}
</script>

<div class="grid items-start gap-6 md:grid-cols-[minmax(0,21rem)_minmax(0,1fr)]">
	<aside class="card grid gap-3">
		<div class="flex items-center gap-3">
			<span class="icon-dot"><Headset size={18} /></span>
			<h2 class="title">{open} waiting for a person</h2>
		</div>
		{#if !list.length}
			<p class="hint">
				Nothing has been handed over yet. Run a scenario that escalates and it will show up here.
			</p>
		{/if}
		{#each list as e (e.id)}
			{@const Who = e.actor_type === 'merchant' ? Store : User}
			<button
				aria-current={cur?.id === e.id}
				onclick={() => (picked = e.id)}
				class="grid gap-1 rounded-2xl border border-line p-3 text-left transition-colors hover:border-face aria-[current=true]:border-face aria-[current=true]:bg-face-soft"
			>
				<span class="flex items-center justify-between gap-2">
					<span class="flex items-center gap-1.5 text-sm font-semibold">
						<Who size={15} />
						{e.actor_type === 'merchant' ? 'Shop' : 'User'}
						{e.actor_id}
						{e.packet.tier}
					</span>
					{#if e.status === 'open'}
						<span class="chip {e.minutes_left < 15 ? 'chip-stop' : 'chip-wait'}">
							<Clock size={12} />
							{e.minutes_left} min left
						</span>
					{:else}
						<span class="chip chip-go"><CircleCheck size={12} /> Resolved</span>
					{/if}
				</span>
				<span class="text-xs text-mute first-letter:uppercase">{words(e.reason)}</span>
			</button>
		{/each}
	</aside>

	{#if cur}
		{@const p = cur.packet}
		<section class="card grid gap-5">
			<div class="flex flex-wrap gap-1.5">
				<span class="chip chip-stop">Why it came here: {words(cur.reason)}</span>
				{#if p.amount}<span class="chip tabular-nums">{inr(p.amount)}</span>{/if}
				<span class="chip">Case {p.case_id}</span>
			</div>
			<h2 class="title text-2xl">{p.issue_summary}</h2>

			<div class="grid gap-2">
				<h3 class="font-semibold">What Saathi already tried</h3>
				{#if !p.tried.length}
					<p class="hint">Nothing. It was handed over before any action.</p>
				{/if}
				{#each p.tried as t}
					{@const ok = t.status === 'done'}
					<div class="flex items-center gap-3 rounded-2xl bg-ground px-3 py-2 text-sm">
						<span
							class="grid size-8 shrink-0 place-items-center rounded-full {ok
								? 'bg-go-soft text-go'
								: 'bg-stop-soft text-stop'}"
						>
							{#if ok}<Check size={15} />{:else if t.decision === 'execute'}<X size={15} />{:else}<Ban
									size={15}
								/>{/if}
						</span>
						<span class="flex-1 font-semibold first-letter:uppercase">{words(t.tool)}</span>
						<span class="text-mute">
							{ok ? 'worked' : words(t.result.error ?? t.result.reason ?? 'not allowed')}
						</span>
					</div>
				{/each}
			</div>

			<p class="flex gap-2 rounded-2xl bg-sun-soft p-3 text-sm text-sun-deep">
				<Lightbulb size={18} class="mt-0.5 shrink-0" />
				<span><b>Suggested next step:</b> {p.recommended_action}</span>
			</p>

			{#if cur.status === 'open'}
				<div class="grid gap-2">
					<label for="reply" class="font-semibold">
						Reply to the {cur.actor_type === 'merchant' ? 'shop' : 'user'}
					</label>
					<textarea id="reply" bind:value={reply} rows="3" class="field"></textarea>
					<div>
						<button class="btn btn-solid" onclick={resolve}>
							<SendHorizontal size={15} /> Send reply and resolve
						</button>
					</div>
				</div>
			{:else}
				<p class="hint">Resolved with: {cur.reply}</p>
			{/if}
		</section>
	{/if}
</div>
