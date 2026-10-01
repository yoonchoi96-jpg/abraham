#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-yoonchoi96-jpg/abraham}"
RUNNER_DIR="${2:-$HOME/actions-runner-abraham}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "Set RUNNER_TOKEN to the short-lived token from GitHub:"
  echo "Repo Settings -> Actions -> Runners -> New self-hosted runner -> macOS"
  echo
  echo "Example:"
  echo '  RUNNER_TOKEN=... ./scripts/setup_github_runner_macos.sh'
  exit 1
fi

command -v curl >/dev/null || { echo "curl is required"; exit 1; }
command -v tar >/dev/null || { echo "tar is required"; exit 1; }

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [[ ! -f .runner ]]; then
  RUNNER_VERSION="${RUNNER_VERSION:-2.329.0}"
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) PACKAGE_ARCH="x64" ;;
    arm64) PACKAGE_ARCH="arm64" ;;
    *) echo "Unsupported macOS architecture: $ARCH"; exit 1 ;;
  esac

  URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-${PACKAGE_ARCH}-${RUNNER_VERSION}.tar.gz"
  echo "Downloading GitHub Actions runner $RUNNER_VERSION ($PACKAGE_ARCH)..."
  curl -fL "$URL" -o runner.tar.gz
  tar xzf runner.tar.gz
  rm runner.tar.gz

  ./config.sh \
    --url "https://github.com/$REPO" \
    --token "$RUNNER_TOKEN" \
    --name "Abraham-Mac" \
    --labels "abraham-mac" \
    --work "_work" \
    --unattended
else
  echo "Runner already configured in $RUNNER_DIR"
fi

echo
echo "Installing runner as a macOS launchd service..."
./svc.sh install
./svc.sh start
./svc.sh status
