#!/usr/bin/env bash
# =============================================================================
# deploy.sh  –  Deploy the QQ Bot adapter into hermes-agent
#
# Usage:
#   ./deploy.sh [HERMES_AGENT_DIR]
#
# If HERMES_AGENT_DIR is not supplied, the script uses ~/.hermes/hermes-agent.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------- #
# Resolve hermes-agent path
# --------------------------------------------------------------------------- #
if [[ $# -ge 1 ]]; then
    AGENT_DIR="$1"
else
    AGENT_DIR="$HOME/.hermes/hermes-agent"
fi

AGENT_DIR="$(cd "${AGENT_DIR}" 2>/dev/null && pwd)" || {
    echo "ERROR: hermes-agent directory not found: ${AGENT_DIR}" >&2
    echo "Usage: ./deploy.sh [/path/to/hermes-agent]" >&2
    exit 1
}

# --------------------------------------------------------------------------- #
# Activate hermes-agent's venv if it exists
# --------------------------------------------------------------------------- #
HERMES_VENV="${AGENT_DIR}/venv"
HERMES_VENV2="${AGENT_DIR}/.venv"

if [[ -f "${HERMES_VENV}/bin/activate" ]]; then
    source "${HERMES_VENV}/bin/activate"
    echo "✓ Activated venv: ${HERMES_VENV}"
elif [[ -f "${HERMES_VENV2}/bin/activate" ]]; then
    source "${HERMES_VENV2}/bin/activate"
    echo "✓ Activated venv: ${HERMES_VENV2}"
fi

echo ""
echo "============================================================"
echo "  Deploying QQ Bot adapter to: ${AGENT_DIR}"
echo "============================================================"
echo ""

configure_qq_credentials() {
    local config_file="$(dirname "${AGENT_DIR}")/config.yaml"
    local answer=""

    echo ""
    read -r -p ">>> Do you want to configure QQ credentials in ${config_file}? [y/N]: " answer
    # Case-insensitive match (compatible with bash 3.x)
    case "${answer}" in
        [Yy]|[Yy][Ee][Ss])
            ;;
        *)
            echo "  [skip] Credentials were not changed."
            return 0
            ;;
    esac

    # Ensure pyyaml is installed (reuse PYTHON_CMD resolved in Step 1)
    if ! "${PYTHON_CMD}" -c "import yaml" 2>/dev/null; then
        echo "  Installing pyyaml..."
        if ! "${PYTHON_CMD}" -m pip install --quiet pyyaml; then
            echo "  ERROR: Failed to install pyyaml. Please run: ${PYTHON_CMD} -m pip install pyyaml" >&2
            return 1
        fi
    fi

    local app_id=""
    local client_secret=""

    read -r -p "  Enter QQ App ID: " app_id
    while [[ -z "${app_id// }" ]]; do
        read -r -p "  App ID cannot be empty, enter QQ App ID: " app_id
    done

    read -r -s -p "  Enter QQ Client Secret: " client_secret
    echo ""
    while [[ -z "${client_secret// }" ]]; do
        read -r -s -p "  Client Secret cannot be empty, enter QQ Client Secret: " client_secret
        echo ""
    done

    "${PYTHON_CMD}" - "${config_file}" "${app_id}" "${client_secret}" <<'PY'
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
app_id = sys.argv[2]
client_secret = sys.argv[3]

try:
    import yaml
except Exception:
    print("  ERROR: PyYAML is required for auto config. Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

if config_path.exists():
    raw = config_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw) if raw.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        print(f"  ERROR: {config_path} root is not a YAML object", file=sys.stderr)
        sys.exit(3)
    data = loaded
else:
    data = {}

platforms = data.get("platforms")
if not isinstance(platforms, dict):
    platforms = {}
    data["platforms"] = platforms

qqbot = platforms.get("qqbot")
if not isinstance(qqbot, dict):
    qqbot = {}
    platforms["qqbot"] = qqbot

qqbot["enabled"] = True
extra = qqbot.get("extra")
if not isinstance(extra, dict):
    extra = {}
qqbot["extra"] = extra

extra["app_id"] = str(app_id)
extra["client_secret"] = str(client_secret)
# Remove camelCase duplicates to avoid ambiguity.
extra.pop("appId", None)
extra.pop("clientSecret", None)

config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"  [ok] Credentials written to {config_path}")
PY
}

# --------------------------------------------------------------------------- #
# 1. Install Python dependencies
# --------------------------------------------------------------------------- #
echo ">>> Step 1/3 – Installing Python dependencies..."

# Determine which python to use (prefer venv python, already activated above)
if [[ -f "${HERMES_VENV}/bin/python" ]]; then
    PYTHON_CMD="${HERMES_VENV}/bin/python"
elif [[ -f "${HERMES_VENV2}/bin/python" ]]; then
    PYTHON_CMD="${HERMES_VENV2}/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
else
    PYTHON_CMD="python"
fi

# Ensure pip is available inside the venv (some venvs are created without pip)
if ! "${PYTHON_CMD}" -m pip --version &>/dev/null; then
    echo "  pip not found in venv, bootstrapping with ensurepip..."
    if "${PYTHON_CMD}" -m ensurepip --upgrade &>/dev/null; then
        echo "  [ok] pip bootstrapped successfully"
    else
        echo "  WARNING: ensurepip failed, will try system pip3 as fallback" >&2
        PYTHON_CMD="python3"
    fi
fi

# Check which packages are missing
echo "  Checking required dependencies..."
MISSING_PKGS=""
for pkg in httpx websockets pyyaml; do
    if ! "${PYTHON_CMD}" -c "import ${pkg}" 2>/dev/null; then
        MISSING_PKGS="${MISSING_PKGS} ${pkg}"
    fi
done

if [[ -z "${MISSING_PKGS}" ]]; then
    echo "  [ok] All dependencies already installed (httpx, websockets, pyyaml)"
else
    echo "  Missing:${MISSING_PKGS}"
    echo "  Installing${MISSING_PKGS}..."
    if "${PYTHON_CMD}" -m pip install --quiet${MISSING_PKGS}; then
        echo "  [ok] Dependencies installed successfully"
    else
        echo "  ERROR: pip install failed" >&2
        echo "  Please install manually:" >&2
        echo "    ${PYTHON_CMD} -m pip install${MISSING_PKGS}" >&2
        exit 1
    fi
fi

# --------------------------------------------------------------------------- #
# 2. Apply source patches
# --------------------------------------------------------------------------- #
echo ""
echo ">>> Step 2/3 – Applying patches to hermes-agent..."
python3 "${SCRIPT_DIR}/patch_hermes.py" "${AGENT_DIR}"

# --------------------------------------------------------------------------- #
# 3. Validate the result
# --------------------------------------------------------------------------- #
echo ""
echo ">>> Step 3/3 – Validating patches..."

QQBOT_FILE="${AGENT_DIR}/gateway/platforms/qqbot.py"
if [[ -f "${QQBOT_FILE}" ]]; then
    echo "  [ok] gateway/platforms/qqbot.py exists"
else
    echo "  ERROR: qqbot.py was not copied!" >&2; exit 1
fi

grep -q 'QQBOT = "qqbot"' "${AGENT_DIR}/gateway/config.py" && \
    echo "  [ok] Platform.QQBOT in gateway/config.py" || \
    { echo "  ERROR: Platform.QQBOT not found in config.py" >&2; exit 1; }

grep -q 'Platform.QQBOT' "${AGENT_DIR}/gateway/run.py" && \
    echo "  [ok] QQBotAdapter registered in gateway/run.py" || \
    { echo "  ERROR: QQBotAdapter not found in run.py" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Done
# --------------------------------------------------------------------------- #
echo ""
if ! configure_qq_credentials; then
    echo "  ERROR: Failed to configure QQ credentials." >&2
    exit 1
fi

echo ""
echo "============================================================"
echo "  Deployment complete!"
echo "============================================================"
echo ""
echo "Credentials (choose one method):"
echo ""
echo "  Option A – Environment variables:"
echo "    export QQBOT_APP_ID='<your-app-id>'"
echo "    export QQBOT_CLIENT_SECRET='<your-client-secret>'"
echo ""
echo "  Option B – config.yaml:"
echo "    platforms:"
echo "      qqbot:"
echo "        enabled: true"
echo "        extra:"
echo "          app_id: '<your-app-id>'"
echo "          client_secret: '<your-client-secret>'"
echo ""
echo "Then start hermes-agent normally."
echo ""
