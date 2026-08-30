.PHONY: setup-ml setup-ctrl test test-ctrl clean

VENV_ML := venv-ml
VENV_CTRL := venv-ctrl

# venv-ml: Python 3.11, used for ML/RL/backend/tests. Safe on any dev machine
# (macOS included) since it has no Mininet/Ryu dependency.
setup-ml:
	python3.11 -m venv $(VENV_ML)
	$(VENV_ML)/bin/pip install --upgrade pip
	$(VENV_ML)/bin/pip install -r requirements-ml.txt

# venv-ctrl: Python 3.9, required for Ryu. Must be run on the Linux host/VM
# that also runs Mininet+OVS (see docs/decisions.md and PROJECT_STATE.md for
# the current controller_backend decision).
setup-ctrl:
	python3.9 -m venv $(VENV_CTRL)
	$(VENV_CTRL)/bin/pip install --upgrade pip
	$(VENV_CTRL)/bin/pip install -r requirements-ctrl.txt

# Unit/integration tests that need no Mininet/Ryu run under venv-ml.
test:
	$(VENV_ML)/bin/python -m pytest tests/unit tests/integration -v --cov=common --cov=database --cov-report=term-missing

# Controller-side tests (of_compat import-hygiene, etc.) run under venv-ctrl
# once Phase 2 creates it.
test-ctrl:
	$(VENV_CTRL)/bin/python -m pytest tests/unit/test_of_compat.py -v

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} +
	rm -rf .pytest_cache .coverage htmlcov
