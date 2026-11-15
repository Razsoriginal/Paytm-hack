<script>
	import { FlaskConical, Handshake, Headset, Play, Store, TrendingUp, User } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import Consumer from './components/Consumer.svelte';
	import Impact from './components/Impact.svelte';
	import Merchant from './components/Merchant.svelte';
	import Ops from './components/Ops.svelte';
	import Scenarios from './components/Scenarios.svelte';
	import { app, start } from './lib/state.svelte.js';

	const roles = [
		['consumer', 'Consumer', User],
		['merchant', 'Merchant', Store],
		['ops', 'Support desk', Headset],
		['impact', 'Impact', TrendingUp],
		['scenarios', 'Scenarios', Play],
	];
	const partnerNames = {
		sarvam_llm: 'Sarvam language',
		sarvam_speech: 'Sarvam voice',
		document_ai: 'Sarvam Document AI',
		cognee: 'Cognee memory',
		paytm_pg: 'Paytm gateway',
	};

	const partners = $derived(Object.entries(app.partners ?? {}));
	const live = $derived(partners.filter(([, p]) => p.mode === 'live').length);

	onMount(start);
</script>

<div class="min-h-dvh" data-face={app.role}>
	<header class="sticky top-0 z-10 border-b border-line bg-white/90 backdrop-blur">
		<div
			class="mx-auto flex max-w-6xl flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 lg:flex-nowrap"
		>
			<div class="flex shrink-0 items-center gap-3">
				<span class="grid size-11 place-items-center rounded-2xl bg-face text-white">
					<Handshake size={24} strokeWidth={2.2} />
				</span>
				<div class="leading-tight">
					<h1 class="text-2xl font-extrabold tracking-tight">Saathi</h1>
					<p class="flex items-center gap-1 text-xs font-semibold whitespace-nowrap text-sun-deep">
						<FlaskConical size={13} /> Simulated data, no real money
					</p>
				</div>
			</div>

			<div
				class="order-last flex w-full gap-1 overflow-x-auto rounded-full bg-ground p-1 lg:order-none lg:mx-auto lg:w-auto lg:shrink-0 lg:overflow-visible"
				role="tablist"
				aria-label="Choose a view"
			>
				{#each roles as [key, label, Icon]}
					<button
						role="tab"
						aria-selected={app.role === key}
						onclick={() => (app.role = key)}
						class="flex items-center gap-1.5 rounded-full px-4 py-2 text-sm font-semibold whitespace-nowrap text-mute transition-colors hover:text-ink aria-selected:bg-white aria-selected:text-face aria-selected:shadow-sm"
					>
						<Icon size={16} />
						{label}
					</button>
				{/each}
			</div>

			<span
				class="ml-auto hidden shrink-0 items-center gap-2 rounded-full border border-line px-3 py-1.5 text-xs font-semibold text-mute sm:flex"
				title={partners.map(([k, p]) => `${partnerNames[k]}: ${p.mode}`).join('\n')}
			>
				<span class="flex gap-1">
					{#each partners as [, p]}
						<span class="size-2 rounded-full {p.mode === 'live' ? 'bg-go' : 'bg-sun'}"></span>
					{/each}
				</span>
				{live} of {partners.length} services live
			</span>
		</div>
	</header>

	<main class="mx-auto max-w-6xl px-3 py-6 sm:px-4">
		{#if app.error}
			<p class="card">{app.error}</p>
		{:else if !app.boot}
			<p class="hint">Loading the simulated world…</p>
		{:else if app.role === 'consumer'}
			<Consumer />
		{:else if app.role === 'merchant'}
			<Merchant />
		{:else if app.role === 'ops'}
			<Ops />
		{:else if app.role === 'impact'}
			<Impact />
		{:else}
			<Scenarios />
		{/if}
	</main>
</div>
