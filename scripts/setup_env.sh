#!/usr/bin/env bash
# Creates venv-ml (Python 3.11) and/or venv-ctrl (Python 3.9 + Ryu).
#
# Usage:
#   scripts/setup_env.sh ml      # any machine
#   scripts/setup_env.sh ctrl    # Linux host/VM only (needs Mininet/OVS too)
#   scripts/setup_env.sh all
#
# venv-ctrl is not a plain `pip install -r requirements-ctrl.txt` because
# Ryu 4.34's sdist uses pbr for versioning, which reports version 0.0.0 when
# built outside a git checkout. That breaks two ways:
#   1. `pip install ryu==4.34` rejects the built wheel because its
#      self-reported version (0.0.0) doesn't match the pin.
#   2. Letting pip use build isolation (a fresh setuptools) instead hits a
#      different failure: Ryu's setup.py calls into
#      setuptools.command.easy_install internals that setuptools >= 60
#      removed.
# The fix (verified 2026-08-31 in the sdn-vm, see docs/decisions.md and
# PROJECT_STATE.md): install our own pinned `setuptools<60` first, disable
# build isolation so Ryu's setup.py uses it, and set PBR_VERSION explicitly
# so the wheel is built with the correct version string.
#
# venv-ctrl is placed OUTSIDE the repo root by default (see CTRL_VENV_DIR
# below), not inside it. When the repo is a multipass "classic" (SSHFS)
# mount from a macOS host into a Linux VM -- our documented Phase 2 setup --
# `python3.9 -m venv` inside the mount produces broken bin/python symlinks
# that even `rm -rf` cannot remove (SSHFS "Value too large for defined data
# type" on stat()). A venv on the VM's native filesystem has no such issue.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RYU_VERSION="4.34"
RYU_SDIST_URL="https://files.pythonhosted.org/packages/25/30/1c1972b3f9bf52de32d2fdf7c5206ff7099e739bf69b6004f4a71bcbde18/ryu-${RYU_VERSION}.tar.gz"
CTRL_VENV_DIR="${CTRL_VENV_DIR:-$HOME/venv-ctrl-ai-self-healing-sdn}"

setup_ml() {
    echo "== venv-ml (Python 3.11) =="
    local py311
    py311="$(command -v python3.11 || echo /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11)"
    if [ ! -x "$py311" ]; then
        echo "python3.11 not found. Install it (e.g. 'brew install python@3.11' on macOS," \
             "or via deadsnakes on Linux) and re-run." >&2
        exit 1
    fi
    "$py311" -m venv venv-ml
    ./venv-ml/bin/pip install --quiet --upgrade pip
    ./venv-ml/bin/pip install --quiet -r requirements-ml.txt
    echo "venv-ml ready."
}

setup_ctrl() {
    echo "== venv-ctrl (Python 3.9 + Ryu ${RYU_VERSION}) =="
    if ! command -v python3.9 >/dev/null 2>&1; then
        echo "python3.9 not found. On Ubuntu 22.04:" >&2
        echo "  sudo add-apt-repository -y ppa:deadsnakes/ppa" >&2
        echo "  sudo apt-get update && sudo apt-get install -y python3.9 python3.9-venv python3.9-dev" >&2
        exit 1
    fi

    echo "venv-ctrl location: $CTRL_VENV_DIR"
    python3.9 -m venv "$CTRL_VENV_DIR"
    "$CTRL_VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$CTRL_VENV_DIR/bin/pip" install --quiet "setuptools<60" wheel

    # eventlet==0.30.2 declares `dnspython<2.0.0,>=1.15.0`; the requested
    # dnspython==2.2.1 conflicts with that (eventlet 0.30.2 predates
    # dnspython 2.x API support), so we install the newest compatible 1.x
    # release instead. See docs/decisions.md (2026-08-31).
    #
    # Installed before the ryu wheel itself (which goes in via --no-deps
    # below, since pip can't resolve deps against a 0.0.0-reporting local
    # build) so pip never reports a spurious "not installed" warning for
    # dependencies we're about to install anyway.
    "$CTRL_VENV_DIR/bin/pip" install --quiet eventlet==0.30.2 dnspython==1.16.0 \
        "msgpack>=0.3.0" netaddr "oslo.config>=2.5.0" "ovs>=2.6.0" \
        routes "six>=1.4.0" tinyrpc "webob>=1.2" networkx numpy requests pyyaml

    build_dir="$(mktemp -d)"
    trap 'rm -rf "$build_dir"' EXIT
    curl -sL -o "$build_dir/ryu.tar.gz" "$RYU_SDIST_URL"
    tar xzf "$build_dir/ryu.tar.gz" -C "$build_dir"
    (
        cd "$build_dir/ryu-${RYU_VERSION}"
        PBR_VERSION="$RYU_VERSION" "$CTRL_VENV_DIR/bin/python3.9" setup.py -q bdist_wheel
    )
    "$CTRL_VENV_DIR/bin/pip" install --quiet --force-reinstall --no-deps \
        "$build_dir/ryu-${RYU_VERSION}/dist/ryu-${RYU_VERSION}-py3-none-any.whl"

    "$CTRL_VENV_DIR/bin/python3.9" -c "from ryu.base import app_manager; print('ryu ok')"
    echo "venv-ctrl ready at $CTRL_VENV_DIR"
}

case "${1:-}" in
    ml) setup_ml ;;
    ctrl) setup_ctrl ;;
    all) setup_ml; setup_ctrl ;;
    *)
        echo "Usage: $0 {ml|ctrl|all}" >&2
        exit 1
        ;;
esac
