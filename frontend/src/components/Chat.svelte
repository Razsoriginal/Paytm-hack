<script>
	import { FileText, Handshake, Mic, Paperclip, SendHorizontal, Undo2, Volume2, X } from '@lucide/svelte';
	import { tick } from 'svelte';
	import { api, listen, say, upload } from '../lib/api.js';
	import { app } from '../lib/state.svelte.js';
	import BillReview from './BillReview.svelte';
	import Receipt from './Receipt.svelte';
	import StockReview from './StockReview.svelte';

	// attach: { purpose: 'bill' | 'stock', path: upload endpoint, label }
	let { type, id, lang = '', name = '', hints = [], attach = null, onchange = () => {} } = $props();
	const key = $derived(`${type}${id}`);
	const msgs = $derived(app.chats[key] ?? []);
	let text = $state('');
	let busy = $state(false);
	let hearing = $state(false);
	let picking = $state(false);
	let samples = $state([]);
	let log = $state();
	let fileInput = $state();

	function push(m) {
		(app.chats[key] ??= []).push(m);
	}

	$effect(() => {
		msgs.length;
		busy;
		tick().then(() => log?.scrollTo({ top: log.scrollHeight, behavior: 'smooth' }));
	});

	function onReply(r) {
		push({ text: r.reply, table: r.table, undo: r.undo });
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

	async function openPicker() {
		picking = !picking;
		if (picking && !samples.length) samples = await api(`/samples?purpose=${attach.purpose}`);
	}

	async function sendFile(source, label) {
		picking = false;
		push({ me: true, text: `Uploaded ${label}`, file: true });
		busy = true;
		try {
			const r = await upload(attach.path, source);
			push({ text: r.reply });
			if (r.fields?.length || r.items?.length) push({ [attach.purpose]: r });
			onchange();
		} catch (e) {
			push({ text: e.message });
		}
		busy = false;
	}

	async function undo(m) {
		try {
			const r = await api(`/merchant/${id}/stock/undo`, 'POST', { movement_id: m.undo });
			m.undo = null;
			push({ text: `Undone. ${r.name} is back to ${r.stock}.` });
			onchange();
		} catch (e) {
			push({ text: `I could not undo that: ${e.message}.` });
		}
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
			{:else if m.bill}
				<div class="pl-9"><BillReview userId={id} review={m.bill} onresult={onReply} /></div>
			{:else if m.stock}
				<div class="pl-9"><StockReview merchantId={id} review={m.stock} onresult={onReply} /></div>
			{:else if m.me}
				<p
					class="flex max-w-[82%] items-center gap-2 self-end rounded-3xl rounded-br-md bg-face px-4 py-2.5 text-sm whitespace-pre-wrap text-white"
				>
					{#if m.file}<FileText size={15} class="shrink-0" />{/if}
					{m.text}
				</p>
			{:else}
				<div class="flex max-w-[94%] items-end gap-2">
					{@render avatar()}
					<div class="grid min-w-0 gap-2 rounded-3xl rounded-bl-md bg-ground px-4 py-2.5 text-sm">
						<p class="whitespace-pre-wrap">{m.text}</p>
						{#if m.table?.rows.length}
							<div class="overflow-x-auto rounded-xl bg-white">
								<table class="w-full text-xs tabular-nums">
									<thead>
										<tr class="text-left text-mute">
											{#each m.table.columns as c}<th class="px-2.5 py-1.5 font-semibold">{c}</th>{/each}
										</tr>
									</thead>
									<tbody>
										{#each m.table.rows as row}
											<tr class="border-t border-line">
												{#each row as cell}<td class="px-2.5 py-1.5">{cell}</td>{/each}
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
						{#if m.undo}
							<button class="btn justify-self-start py-1" onclick={() => undo(m)}>
								<Undo2 size={14} /> Undo
							</button>
						{/if}
					</div>
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

	{#if picking}
		<div class="grid max-h-64 gap-2 overflow-auto border-t border-line bg-ground px-4 py-3">
			<div class="flex items-center justify-between">
				<p class="text-sm font-semibold">{attach.label}</p>
				<button class="text-mute" aria-label="Close" onclick={() => (picking = false)}>
					<X size={16} />
				</button>
			</div>
			<button class="btn btn-solid" onclick={() => fileInput.click()}>
				<Paperclip size={15} /> Choose a photo or PDF
			</button>
			<input
				bind:this={fileInput}
				type="file"
				accept="application/pdf,image/png,image/jpeg"
				capture="environment"
				class="hidden"
				onchange={(e) => {
					const f = e.currentTarget.files[0];
					if (f) sendFile({ file: f }, f.name);
					e.currentTarget.value = '';
				}}
			/>
			<p class="hint text-xs">Or try a made-up sample:</p>
			{#each samples as sm}
				<button
					class="flex items-center gap-2 rounded-xl bg-white px-3 py-2 text-left text-sm hover:text-face"
					onclick={() => sendFile({ sample: sm.name }, sm.label.toLowerCase())}
				>
					<FileText size={15} class="shrink-0 text-face" />
					{sm.label}
				</button>
			{/each}
		</div>
	{/if}

	<form
		class="flex items-center gap-2 border-t border-line px-3 pt-3 pb-2"
		onsubmit={(e) => {
			e.preventDefault();
			send(text);
		}}
	>
		{#if attach}
			<button
				type="button"
				onclick={openPicker}
				aria-label={attach.label}
				class="grid size-10 shrink-0 place-items-center rounded-full bg-face-soft text-face transition-colors hover:bg-face hover:text-white"
			>
				<Paperclip size={18} />
			</button>
		{/if}
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
