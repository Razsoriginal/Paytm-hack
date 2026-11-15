"""`make doctor`: key presence and connectivity per service. Never prints a key value."""
from .adapters.llm import llm
from .adapters.memory import memory
from .adapters.paytm_pg import pg
from .adapters.speech import speech
from .config import settings

for k, present in settings.key_report().items():
	print(f"{k:22} {'PRESENT' if present else 'MISSING'}")
for name, svc in (("Sarvam LLM", llm), ("Sarvam speech", speech), ("Cognee memory", memory),
		("Paytm PG", pg)):
	h = svc.health()
	print(f"{name:22} {h['mode'].upper():5} {'ok' if h['ok'] else 'FAILING'}  {h['detail']}")
