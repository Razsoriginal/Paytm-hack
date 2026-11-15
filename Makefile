VENV ?= .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
export PYTHONPATH := backend

# The virtualenv is created and filled on first use, and refreshed when requirements change.
$(STAMP): backend/requirements.txt
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r backend/requirements.txt
	touch $(STAMP)

install: $(STAMP)
seed: $(STAMP)
	$(PY) -m saathi.seed
ui:
	cd frontend && npm install --no-audit --no-fund && npm run build
frontend/dist/index.html:
	$(MAKE) ui
dev: $(STAMP) frontend/dist/index.html
	$(PY) -m uvicorn saathi.api:app --reload --port $${BACKEND_PORT:-8000}
dev-ui:
	cd frontend && npm run dev
demo: seed dev
test: $(STAMP)
	cd backend && ../$(PY) -m pytest -q
eval: $(STAMP)
	DATABASE_URL=sqlite:///data/eval.db SARVAM_MODE=mock LLM_MODE=mock $(PY) -m saathi.evaluate
doctor: $(STAMP)
	$(PY) -m saathi.doctor
clean:
	rm -rf $(VENV) frontend/node_modules frontend/dist data
.PHONY: install seed ui dev dev-ui demo test eval doctor clean
