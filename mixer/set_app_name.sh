#!/usr/bin/env bash
# Name of the /mixer home-screen app on phones (Chrome "Install app"), for THIS Pi only.
# Stored in ~/stage-messenger/mixer_config.json (not in git). Takes effect immediately -- no restart.
#   bash ~/stage-messenger/mixer/set_app_name.sh "Presley Mixer"            # name (also the icon label)
#   bash ~/stage-messenger/mixer/set_app_name.sh "Presley's X32 Mixer" "Presley"   # full name + short icon label
#   bash ~/stage-messenger/mixer/set_app_name.sh --clear                    # back to "Mixer"
# Phones that already installed the app: uninstall it and install again to pick up a new name now.
set -euo pipefail
CFG="$(cd "$(dirname "$0")/.." && pwd)/mixer_config.json"
[ $# -ge 1 ] || { sed -n 2,8p "$0"; exit 1; }
python3 - "$CFG" "$@" <<'PY'
import json, os, sys
p, args = sys.argv[1], sys.argv[2:]
c = json.load(open(p)) if os.path.exists(p) else {}
if args[0] == '--clear':
    c.pop('app_name', None); c.pop('app_short_name', None)
else:
    c['app_name'] = args[0].strip()[:45]
    if len(args) > 1 and args[1].strip(): c['app_short_name'] = args[1].strip()[:20]
    else: c.pop('app_short_name', None)
tmp = p + '.tmp'
json.dump(c, open(tmp, 'w'), indent=2); os.replace(tmp, p); os.chmod(p, 0o600)
print('app name:', c.get('app_name', 'Mixer (default)'), '| icon label:', c.get('app_short_name') or c.get('app_name') or 'Mixer')
PY
