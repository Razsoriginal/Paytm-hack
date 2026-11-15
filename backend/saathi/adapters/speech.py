"""Sarvam speech-to-text, text-to-speech and translation, each with a mock fallback."""
import base64
import hashlib
import io
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..config import settings

LANGS = {"hi-IN", "mr-IN", "kn-IN", "en-IN"}


def _headers() -> dict:
	return {"api-subscription-key": settings.SARVAM_API_KEY}


def _live() -> bool:
	return settings.SARVAM_MODE == "live" and bool(settings.SARVAM_API_KEY)


def _silence(seconds: float = 0.25) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, "wb") as w:
		w.setnchannels(1)
		w.setsampwidth(2)
		w.setframerate(22050)
		w.writeframes(b"\x00\x00" * int(22050 * seconds))
	return buf.getvalue()


def _join_wavs(parts: list[bytes]) -> bytes:
	out, params, frames = io.BytesIO(), None, []
	for p in parts:
		with wave.open(io.BytesIO(p), "rb") as w:
			params = params or w.getparams()
			frames.append(w.readframes(w.getnframes()))
	with wave.open(out, "wb") as w:
		w.setparams(params)
		for f in frames:
			w.writeframes(f)
	return out.getvalue()


class Speech:
	def stt(self, audio: bytes, language: str = "", hint: str = "") -> dict:
		"""Returns {transcript, language_code, mode}. Mock mode echoes the typed hint."""
		if _live() and audio:
			try:
				data = {"model": settings.SARVAM_STT_MODEL, "mode": "transcribe"}
				if language in LANGS:
					data["language_code"] = language
				r = httpx.post(f"{settings.SARVAM_BASE_URL}/speech-to-text", headers=_headers(),
					data=data, files={"file": ("audio.wav", audio, "audio/wav")}, timeout=30)
				r.raise_for_status()
				j = r.json()
				return {"transcript": j.get("transcript", ""), "mode": "live",
					"language_code": j.get("language_code") or language}
			except Exception:
				pass
		return {"transcript": hint, "language_code": language or settings.SARVAM_DEFAULT_LANGUAGE,
			"mode": "mock"}

	def _tts_one(self, sentence: str, language: str) -> bytes:
		cache = Path(settings.SARVAM_AUDIO_CACHE_DIR)
		cache.mkdir(parents=True, exist_ok=True)
		key = hashlib.sha256(f"{settings.SARVAM_TTS_MODEL}|{settings.SARVAM_TTS_SPEAKER}|"
			f"{language}|{sentence}".encode()).hexdigest()[:32]
		f = cache / f"{key}.wav"
		if f.exists():
			return f.read_bytes()
		body = {"text": sentence, "target_language_code": language,
			"model": settings.SARVAM_TTS_MODEL}
		if settings.SARVAM_TTS_SPEAKER:
			body["speaker"] = settings.SARVAM_TTS_SPEAKER
		r = httpx.post(f"{settings.SARVAM_BASE_URL}/text-to-speech", headers=_headers(), json=body,
			timeout=30)
		r.raise_for_status()
		wav = base64.b64decode(r.json()["audios"][0])
		f.write_bytes(wav)
		return wav

	def tts(self, text: str, language: str = "") -> dict:
		"""Split by sentence, synthesise in parallel, return one WAV in the original order."""
		language = language if language in LANGS else settings.SARVAM_DEFAULT_LANGUAGE
		sentences = [x.strip() for x in re.split(r"(?<=[.!?।])\s+", text) if x.strip()]
		if _live() and sentences:
			try:
				with ThreadPoolExecutor(max_workers=4) as pool:
					parts = list(pool.map(lambda x: self._tts_one(x, language), sentences))
				return {"wav": _join_wavs(parts), "mode": "live"}
			except Exception:
				pass
		# Mock: silent WAV. The UI falls back to the browser's own speech synthesis.
		return {"wav": _silence(), "mode": "mock"}

	def translate(self, text: str, target: str, source: str = "auto") -> dict:
		if _live() and text.strip():
			try:
				r = httpx.post(f"{settings.SARVAM_BASE_URL}/translate", headers=_headers(), timeout=20,
					json={"input": text, "source_language_code": source,
						"target_language_code": target})
				r.raise_for_status()
				return {"text": r.json().get("translated_text", text), "mode": "live"}
			except Exception:
				pass
		return {"text": text, "mode": "mock"}

	def health(self) -> dict:
		if not _live():
			return {"mode": "mock", "ok": True, "detail": "typed transcript, browser voice"}
		out = self.translate("namaste", "en-IN")
		return {"mode": out["mode"], "ok": out["mode"] == "live",
			"detail": "translate round-trip" if out["mode"] == "live" else "request failed"}


speech = Speech()
