.PHONY: setup-ml setup-ctrl test test-ctrl clean

# venv-ctrl defaults to a path OUTSIDE the repo (see scripts/setup_env.sh):
# on the documented macOS-host + multipass-VM setup, a venv created inside
# the SSHFS-mounted repo gets broken bin/python symlinks. Override with
# CTRL_VENV_DIR=./venv-ctrl if you're on a Linux host with no such mount.
VENV_ML := venv-ml
VENV_CTRL := $(if $(CTRL_VENV_DIR),$(CTRL_VENV_DIR),$(HOME)/venv-ctrl-ai-self-healing-sdn)

# venv-ml: Python 3.11, used for ML/RL/backend/tests. Safe on any dev machine
# (macOS included) since it has no Mininet/Ryu dependency.
setup-ml:
	scripts/setup_env.sh ml

# venv-ctrl: Python 3.9, required for Ryu. Must be run on the Linux host/VM
# that also runs Mininet+OVS (see docs/decisions.md and PROJECT_STATE.md for
# the current controller_backend decision). See scripts/setup_env.sh for why
# this isn't a plain `pip install -r requirements-ctrl.txt`.
setup-ctrl:
	scripts/setup_env.sh ctrl

# Unit/integration tests that need no Mininet/Ryu run under venv-ml.
test:
	$(VENV_ML)/bin/python -m pytest tests/unit tests/integration -v --cov=common --cov=database --cov-report=term-missing

# pytest is not part of the approved venv-ctrl stack (see
# requirements-ctrl.txt); the of_compat hygiene test is pure AST parsing
# and already runs under `make test` via venv-ml. This target instead does
# the one check that actually needs the real ryu install: that of_compat.py
# resolves BACKEND against it.
test-ctrl:
	$(VENV_CTRL)/bin/python3.9 -c "from controller.of_compat import BACKEND; print('of_compat backend:', BACKEND)"

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} +
	rm -rf .pytest_cache .coverage htmlcov
