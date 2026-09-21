#!/bin/bash
# Wrap stock hermeswebui_init so we can patch /app after the root rsync and
# before python server.py. See patch-webui-persisted-count.py.
set -euo pipefail

INIT_SRC=/hermeswebui_init.bash
INIT_WRAPPED=/tmp/hermeswebui_init_wrapped.bash
PATCH_COUNT=/bootstrap/patch-webui-persisted-count.py
PATCH_LIBRARY=/bootstrap/patch-webui-projects-library.py

cp "$INIT_SRC" "$INIT_WRAPPED"

# Insert runtime patches immediately before the server starts. The stock
# image re-execs this script as hermeswebui after syncing /apptoo → /app, so
# patching here lands on the runtime tree the server imports.
python3 - <<'PY'
from pathlib import Path
path = Path("/tmp/hermeswebui_init_wrapped.bash")
text = path.read_text()
needle = 'echo ""; echo "== Running hermes-webui"'
inject = '''if [ -f /bootstrap/patch-webui-persisted-count.py ]; then
  python3 /bootstrap/patch-webui-persisted-count.py /app/api/background_process.py || echo "!! WARNING: webui persisted-count patch failed (continuing)"
fi
if [ -f /bootstrap/patch-webui-projects-library.py ]; then
  python3 /bootstrap/patch-webui-projects-library.py /app/api/workspace.py || echo "!! WARNING: webui projects-library patch failed (continuing)"
fi
echo ""; echo "== Running hermes-webui"'''
if "patch-webui-projects-library.py" in text and "patch-webui-persisted-count.py" in text:
    print("[hermeswebui-entrypoint] wrapper already has patch hooks", flush=True)
elif needle not in text:
    raise SystemExit("hermeswebui_init.bash server-start marker not found; refusing to start without patch hook")
else:
    path.write_text(text.replace(needle, inject, 1))
    print("[hermeswebui-entrypoint] inserted webui patch hooks", flush=True)
PY

# Also patch /apptoo early when writable so a later root rsync carries the fix.
if [ -f "$PATCH_COUNT" ] && [ -f /apptoo/api/background_process.py ]; then
  python3 "$PATCH_COUNT" /apptoo/api/background_process.py || echo "!! WARNING: apptoo persisted-count patch failed (continuing)"
fi
if [ -f "$PATCH_LIBRARY" ] && [ -f /apptoo/api/workspace.py ]; then
  python3 "$PATCH_LIBRARY" /apptoo/api/workspace.py || echo "!! WARNING: apptoo projects-library patch failed (continuing)"
fi

exec bash "$INIT_WRAPPED"
