#!/usr/bin/env bash
#
# a3watch bootstrap installer.
#
# Step 1 of the two-step, review-gated install:
#   1. this script: place the agent on the NVMe, install a launcher, run
#      non-waking detection, and write an EDITABLE config.
#   2. you review /etc/a3watch/config.toml (roles, protected drives, interval,
#      data dir, CORS origins, tunnel hostname).
#   3. `sudo a3watch install --confirm` wires the systemd timer + API socket
#      and installs the (dormant) diagnostic tools.
#
# This script makes NO systemd changes and installs NO packages.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo (it installs to /opt and /usr/local/bin)." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/a3watch

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (Ubuntu ships it)." >&2
  exit 1
fi
PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' || {
  echo "python3 >= 3.11 required (found $PYVER) for tomllib." >&2; exit 1; }

echo "Installing a3watch agent to $DEST (python $PYVER)…"
install -d "$DEST"
rm -rf "$DEST/a3watch"
cp -r "$SRC_DIR/a3watch" "$DEST/a3watch"

cat > /usr/local/bin/a3watch <<'EOF'
#!/usr/bin/env bash
export PYTHONPATH=/opt/a3watch${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m a3watch "$@"
EOF
chmod 0755 /usr/local/bin/a3watch

echo
echo "Running non-waking detection (this will NOT spin up any disk)…"
echo
a3watch detect

echo
echo "Next:"
echo "  1) Review/edit  /etc/a3watch/config.toml"
echo "  2) Apply:       sudo a3watch install --confirm"
