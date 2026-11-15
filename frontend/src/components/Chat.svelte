<script>
	import { Handshake, Mic, SendHorizontal, Volume2 } from '@lucide/svelte';
	import { tick } from 'svelte';
	import { api, listen, say } from '../lib/api.js';
	import { app } from '../lib/state.svelte.js';
	import Receipt from './Receipt.svelte';

	let { type, id, lang = '', name = '', hints = [], onchange = () => {} } = $props();
	const key = $derived(`${type}${id}`);
	const msgs = $derived(app.chats[key] ?? []);
	let text = $state('');
	let busy = $state(false);
	let hearing = $state(false);
	let log = $state();

	function push(m) {
		(app.chats[key] ??= []).push(m);
	}

	$effect(() => {
		msgs.length;
		busy;
		tick().then(() => log?.scrollTo({ top: log.scrollHeight, behavior: 'smooth' }));
	});

	function onReply(r) {
		push({ text: r.reply });
		if (r.approval) push({ approval: r.approval, alt: r.alternative });
		if (app.speak) say(r.reply, r.language);
		onchange();
	}

	async function send(message, spoken = false) {
		const t = message.trim();
		if (!t || busy) return;
		push({ me: true, text: t });
		text = '';
		busy = true;
		try {
			if (spoken) {
				const f = new FormData();
				f.append('actor_type', type);
				f.append('actor_id', id);
				f.append('language', lang);
				f.append('hint', t);
				onReply(await (await fetch('/api/voice', { method: 'POST', body: f })).json());
			} else {
				onReply(await api('/chat', 'POST', { actor_type: type, actor_id: id, text: t }));
			}
		} catch (e) {
			push({ text: `That message did not reach Saathi (${e.message}). Send it again.` });
		}
		busy = false;
	}

	function speak() {
		hearing = true;
		listen(
			lang,
			(t) => {
				hearing = false;
				send(t, true);
			},
			(why) => {
				hearing = false;
				push({ text: why });
			},
		);
	}
</script>

{#snippet avatar()}
	<span class="grid size-7 shrink-0 place-items-center rounded-full bg-face text-white">
		<Handshake size={15} />
	</span>
{/snippet}

<div class="flex min-h-0 flex-1 flex-col">
	<div class="flex flex-1 flex-col gap-3 overflow-auto px-4 py-4" bind:this={log} aria-live="polite">
		{#if !msgs.length}
			<div class="my-auto grid gap-4">
				<div>
					<p class="font-display text-2xl leading-tight font-extrabold">
						Namaste{name ? `, ${name}` : ''}.
					</p>
					<p class="hint mt-1">Tell me what you need, in Hindi, Hinglish or English.</p>
				</div>
				<div class="grid gap-2">
					{#each hints as [Icon, label, example]}
						<button
							onclick={() => send(example)}
							class="flex items-center gap-3 rounded-2xl border border-line p-3 text-left transition-colors hover:border-face hover:bg-face-soft"
						>
							<span class="icon-dot"><Icon size={18} /></span>
							<span class="leading-tight">
								<span class="block text-sm font-semibold">{label}</span>
								<span class="text-xs text-mute">“{example}”</span>
							</span>
						</button>
					{/each}
				</div>
			</div>
		{/if}

		{#each msgs as m}
			{#if m.approval}
				<div class="pl-9"><Receipt approval={m.approval} alt={m.alt} onresult={onReply} /></div>
			{:else if m.me}
				<p
					class="max-w-[82%] self-end rounded-3xl rounded-br-md bg-face px-4 py-2.5 text-sm whitespace-pre-wrap text-white"
				>
					{m.text}
				</p>
			{:else}
				<div class="flex max-w-[92%] items-end gap-2">
					{@render avatar()}
					<p class="rounded-3xl rounded-bl-md bg-ground px-4 py-2.5 text-sm whitespace-pre-wrap">
						{m.text}
					</p>
				</div>
			{/if}
		{/each}

		{#if busy}
			<div class="flex items-center gap-2 text-sm text-mute">
				{@render avatar()}
				<span class="flex gap-1 rounded-3xl bg-ground px-4 py-3.5" aria-label="Saathi is working on it">
					{#each [0, 150, 300] as d}
						<span
							class="size-1.5 animate-bounce rounded-full bg-mute motion-reduce:animate-none"
							style:animation-delay="{d}ms"
						></span>
					{/each}
				</span>
			</div>
		{/if}
	</div>

	<form
		class="flex items-center gap-2 border-t border-line px-3 pt-3 pb-2"
		onsubmit={(e) => {
			e.preventDefault();
			send(text);
		}}
	>
		<button
			type="button"
			onclick={speak}
			aria-label="Speak your message"
			class="grid size-10 shrink-0 place-items-center rounded-full transition-colors {hearing
				? 'animate-pulse bg-sun text-ink'
				: 'bg-face-soft text-face hover:bg-face hover:text-white'}"
		>
			<Mic size={18} />
		</button>
		<input
			bind:value={text}
			placeholder={hearing ? 'Listening…' : 'Message Saathi'}
			aria-label="Message Saathi"
			class="field flex-1 rounded-full bg-ground px-4"
		/>
		<button
			class="grid size-10 shrink-0 place-items-center rounded-full bg-face text-white transition-colors hover:bg-face-deep disabled:opacity-40"
			disabled={busy || !text.trim()}
			aria-label="Send message"
		>
			<SendHorizontal size={18} />
		</button>
	</form>
	<label class="flex items-center gap-1.5 px-4 pb-2 text-xs text-mute">
		<input type="checkbox" bind:checked={app.speak} class="accent-face" />
		<Volume2 size={13} /> Read replies aloud
	</label>
</div>
