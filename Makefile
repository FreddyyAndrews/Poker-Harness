.PHONY: venv install demo test validate clean

PYTHON ?= python3.10
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	@echo ">> Installing Cython<3 (eval7 build dep)"
	$(PIP) install "Cython<3"
	@echo ">> Installing eval7 with --no-build-isolation"
	$(PIP) install --no-build-isolation eval7==0.1.7
	@echo ">> Installing the arena package (editable) + dev/demo extras"
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
