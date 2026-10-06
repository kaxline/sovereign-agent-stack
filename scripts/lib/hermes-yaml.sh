# Edit Hermes profile YAML with the PyYAML that ships in the Hermes image.
# Source from repo-root scripts (cwd = repo root):
#   source "${ROOT}/scripts/lib/hermes-yaml.sh"
#
# hermes_python ARGS... runs python3 from the pinned Hermes image with
# data/hermes mounted at /opt/data, reading the script from stdin. It needs no
# running container and no compose project name, so it behaves the same under
# a custom -p / COMPOSE_PROJECT_NAME and before the stack's first start.

hermes_python() {
  local tag="${HERMES_AGENT_IMAGE_TAG:-}"
  if [[ -z "$tag" && -f .env ]]; then
    tag="$(grep -E '^HERMES_AGENT_IMAGE_TAG=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
  tag="${tag:-v2026.9.14}"
  docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    -v "$(pwd)/data/hermes:/opt/data" \
    --entrypoint python3 \
    "nousresearch/hermes-agent:${tag}" - "$@"
}

# hermes_set_platform_disabled PROFILE PLATFORM
# Pins platforms.<PLATFORM>.enabled=false in profiles/<PROFILE>/config.yaml.
# No-op when the file is missing. Merges into an existing platforms: mapping.
hermes_set_platform_disabled() {
  hermes_python "$1" "$2" <<'PY'
import sys
from pathlib import Path
import yaml

# Older setup runs appended a second platforms: block when hermes was not
# running. Plain safe_load keeps only the last one, so merge duplicate keys.
class MergingLoader(yaml.SafeLoader):
    pass

def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(out[k], v) if k in out else v
        return out
    return b

def construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=True)
        out[key] = merge(out[key], value) if key in out else value
    return out

MergingLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)

name, platform = sys.argv[1], sys.argv[2]
path = Path(f"/opt/data/profiles/{name}/config.yaml")
if not path.is_file():
    raise SystemExit(0)
raw = path.read_text()
data = yaml.load(raw, Loader=MergingLoader) or {}
# Rewrite when the file had duplicate top-level keys, even if nothing changed.
dupes = len(yaml.compose(raw).value) != len(data) if raw.strip() else False
platforms = data.get("platforms")
if not isinstance(platforms, dict):
    platforms = {}
entry = platforms.get(platform)
if not isinstance(entry, dict):
    entry = {}
if entry.get("enabled") is False and not dupes:
    print(f"platforms.{platform}.enabled already false on {name}")
    raise SystemExit(0)
entry["enabled"] = False
platforms[platform] = entry
data["platforms"] = platforms
path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
print(f"pinned platforms.{platform}.enabled=false on {name}")
PY
}
