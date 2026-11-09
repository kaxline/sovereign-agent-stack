#!/command/with-contenv sh
# Install the Bitwarden broker's Hermes plugin (bwbroker) into Hermes's
# bundled plugins dir, with its backend module beside __init__.py. It is
# installed whether or not BITWARDEN_ENABLED is on, so turning the add-on on
# or off needs no hermes recreate: the plugin loads only on a profile whose
# plugins.enabled lists it, and the bootstraps write that from the flag.
#
# Hermes accepts a login backend only from a bundled plugin or a catalog
# install at tier verified or official, and no config key relaxes that
# (hermes_cli/plugins_cmd_catalog.py login_backend_provenance_error). A copy
# under $HERMES_HOME/plugins would load its tools but have the backend
# refused, so the bootstrap removes any such copy.
set -u
src=/bootstrap/plugins/bwbroker
backend=/bootstrap/bitwarden_broker_backend.py
dst=/opt/hermes/plugins/bwbroker
rm -rf "$dst"
if [ -f "$src/plugin.yaml" ] && [ -f "$src/__init__.py" ] && [ -f "$backend" ]; then
  mkdir -p "$dst" \
    && cp "$src/plugin.yaml" "$src/__init__.py" "$backend" "$dst/" \
    && chmod 755 "$dst" && chmod 644 "$dst"/* \
    || echo "warn: could not install the bwbroker plugin"
else
  echo "warn: bwbroker plugin files missing under /bootstrap; not installed"
fi
