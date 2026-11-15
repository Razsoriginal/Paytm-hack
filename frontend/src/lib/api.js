export async function api(path, method = 'GET', body) {
	const r = await fetch(`/api${path}`, {
		method,
		headers: body ? { 'Content-Type': 'application/json' } : {},
		body: body ? JSON.stringify(body) : undefined,
	});
	if (!r.ok) {
		const detail = (await r.json().catch(() => ({}))).detail;
		throw new Error(detail || r.statusText);
	}
	return r.json();
}

export const inr = (n) => (n == null ? '–' : `₹${Math.round(n).toLocaleString('en-IN')}`);
export const pct = (n) => (n == null ? '–' : `${Math.round(n * 10) / 10}%`);

/** Sarvam voice when it is live, the browser's own voice otherwise. */
export async function say(text, lang) {
	try {
		const r = await fetch(`/api/tts?text=${encodeURIComponent(text)}&language=${lang || ''}`);
		if (r.headers.get('X-Saathi-Mode') === 'live') {
			await new Audio(URL.createObjectURL(await r.blob())).play();
			return;
		}
	} catch {}
	const u = new SpeechSynthesisUtterance(text);
	u.lang = lang || 'hi-IN';
	speechSynthesis.speak(u);
}

export function listen(lang, onText, onError) {
	const R = window.SpeechRecognition || window.webkitSpeechRecognition;
	if (!R) return onError('Voice input is not available in this browser. Type your message.');
	const rec = new R();
	rec.lang = lang || 'hi-IN';
	rec.onresult = (e) => onText(e.results[0][0].transcript);
	rec.onerror = () => onError('I could not hear that. Try again or type your message.');
	rec.start();
}

/** Send a picked file, or the name of a seeded sample, as multipart form data. */
export async function upload(path, { file, sample }) {
	const form = new FormData();
	if (file) form.append('file', file);
	else form.append('sample', sample);
	const r = await fetch(`/api${path}`, { method: 'POST', body: form });
	if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
	return r.json();
}
