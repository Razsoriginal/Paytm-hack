import { api } from './api.js';

export const app = $state({
	role: 'consumer',
	boot: null,
	partners: null,
	uid: 1,
	mid: 1,
	chats: {},
	speak: false,
	runs: [],
	fresh: 0, // runs with id above this arrived while the page was open
	error: '',
});

let source;

export async function loadPartners() {
	app.partners = await api('/partners');
}

/** Live agent timeline over server-sent events. */
export async function connectRuns() {
	source?.close();
	const past = await api('/runs?limit=60');
	app.runs = past;
	app.fresh = past.at(-1)?.id ?? 0;
	source = new EventSource(`/api/runs/stream?after=${app.fresh}`);
	source.onmessage = (e) => {
		const run = JSON.parse(e.data);
		if (!app.runs.some((r) => r.id === run.id)) app.runs.push(run);
		if (app.runs.length > 200) app.runs.splice(0, app.runs.length - 200);
	};
}

export async function start() {
	try {
		app.boot = await api('/bootstrap');
		app.partners = app.boot.partners;
		await connectRuns();
	} catch (e) {
		app.error = `The Saathi server is not answering (${e.message}). Start it with "make dev".`;
	}
}

export async function resetAll() {
	await api('/reset', 'POST');
	app.chats = {};
	await connectRuns();
}
