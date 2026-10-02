.PHONY: venv install demo test validate clean

# newest supported interpreter on PATH (eval7 wheels: see README "Install")
PYTHON ?= $(shell command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install -e ".[dev,demo]"

demo:
	$(PY) demo.py

test:
	$(PY) -m pytest -q

validate:
	@if [ -z "$(BOT)" ]; then echo "usage: make validate BOT=bots/mybot/bot.py"; exit 1; fi
	$(PY) sandbox/validator.py $(BOT)

clean:
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
	find . -type f -name "*.pyc" -not -path "./$(VENV)/*" -delete
