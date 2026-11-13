#!/bin/sh
# Fresh install: hermes-*-bootstrap creates its profile when profiles/<name>/
# already exists as an empty shell.
#
# Since Hermes v0.23.1, `hermes profile create` refuses a directory that has no
# profile identity file. Two things leave such a shell behind on a fresh
# install: setup.sh used to mkdir profiles/api-server and profiles/browser, and
# hermes' memories/ bind mounts create profiles/<name>/memories on the host
# whenever the container starts. The bootstrap removes a shell that holds only
# empty directories and refuses one that holds a file.
#
# On a fresh install the bootstraps run before hermes has ever started, so they
# seed config.yaml themselves from the image, as its first boot would, and
# create the profiles before the bind mounts can leave a shell.
#
# The host part runs the whole bootstrap with stub `hermes` and `python3`; the
# stub refuses an existing directory the way v0.23.1 does. The image part runs
# the bootstrap's cleanup and the real `hermes profile create` in the pinned
# image. Pass --host-only to skip it, --image to require it.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
# shellcheck source=lib/hermes-image.sh
. "${ROOT}/scripts/lib/hermes-image.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
REAL_PYTHON="$(command -v python3)"
BOOTSTRAP_SH="${BOOTSTRAP_SH:-$(command -v dash || echo sh)}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --- setup.sh leaves profile directories to Hermes ---
if grep -nE '"(\$\{?[A-Z_]+\}?/)?data/hermes/profiles/' "${ROOT}/scripts/setup.sh" >&2; then
  fail "setup.sh creates a directory under data/hermes/profiles/; Hermes v0.23.1 then refuses to create that profile"
fi

# --- host part: the whole bootstrap against a scratch HERMES_HOME ---
mkdir -p "${tmp}/bin" "${tmp}/bootstrap" "${tmp}/addon-data/twenty/instances" \
  "${tmp}/addon-data/twenty/secrets" "${tmp}/addon-data/caldav/instances"
printf 'API_SERVER_KEY=api-key\n' > "${tmp}/bootstrap/api-server.env"

# Lists the directories that carry the stub identity file; create refuses an
# existing directory, as v0.23.1 refuses one without an identity file.
cat > "${tmp}/bin/hermes" <<EOF
#!/bin/sh
case "\$*" in
  "profile list")
    for d in "\${HERMES_HOME}"/profiles/*/; do
      [ -f "\${d}.identity" ] && basename "\$d"
    done
    exit 0 ;;
  "profile create "*)
    dir="\${HERMES_HOME}/profiles/\$3"
    if [ -e "\$dir" ]; then
      echo "Cannot create profile '\$3': \$dir exists but carries no profile identity file" >&2
      exit 1
    fi
    mkdir -p "\$dir" && touch "\${dir}/.identity" && echo "created \$3" >> "${tmp}/created"
    exit 0 ;;
esac
exit 0
EOF
cat > "${tmp}/bin/python3" <<EOF
#!/bin/sh
[ "\${1:-}" = "-c" ] && exec "$REAL_PYTHON" "\$@"
cat >/dev/null
exit 0
EOF
chmod +x "${tmp}/bin/hermes" "${tmp}/bin/python3"

# fresh_home: a HERMES_HOME as hermes setup leaves it
fresh_home() {
  rm -rf "${tmp:?}/home" "${tmp:?}/created"
  mkdir -p "${tmp}/home/memories"
  printf 'model:\n  default: test\n' > "${tmp}/home/config.yaml"
}

run_bootstrap() {
  _status=0
  env -i PATH="${tmp}/bin:/usr/bin:/bin:/usr/local/bin" \
    HERMES_HOME="${tmp}/home" \
    HERMES_API_PROFILE_NAME=api-server \
    BOOTSTRAP_API_ENV="${tmp}/bootstrap/api-server.env" \
    ADDON_DATA_DIR="${tmp}/addon-data" \
    HERMES_AGENT_IMAGE_TAG=v0.23.1 \
    HERMES_INSTALL_DIR="${tmp}/install" \
    "$BOOTSTRAP_SH" "$bootstrap" > "${tmp}/out" 2>&1 || _status=$?
  return "$_status"
}

# The image's first-boot seeds (stage2-hook.sh seed_one).
mkdir -p "${tmp}/install/docker"
printf 'model:\n  default: seeded\n' > "${tmp}/install/cli-config.yaml.example"
printf 'SEEDED=1\n' > "${tmp}/install/.env.example"
printf 'seeded soul\n' > "${tmp}/install/docker/SOUL.md"

# hermes has never started: the bootstrap seeds the default profile and
# creates its own, so hermes' bind mounts find the profile already there.
rm -rf "${tmp:?}/home" "${tmp:?}/created"
mkdir -p "${tmp}/home"
run_bootstrap || { cat "${tmp}/out" >&2; fail "bootstrap failed on a home hermes never started in"; }
grep -q 'default: seeded' "${tmp}/home/config.yaml" 2>/dev/null \
  || fail "bootstrap did not seed config.yaml from the image"
grep -q 'SEEDED=1' "${tmp}/home/.env" 2>/dev/null || fail "bootstrap did not seed .env from the image"
[ -f "${tmp}/home/SOUL.md" ] || fail "bootstrap did not seed SOUL.md from the image"
grep -q 'created api-server' "${tmp}/created" 2>/dev/null \
  || fail "bootstrap did not create profile api-server on a home hermes never started in"

# An empty shell with the memories/ mount target in it, as hermes leaves it.
fresh_home
mkdir -p "${tmp}/home/profiles/api-server/memories"
run_bootstrap || { cat "${tmp}/out" >&2; fail "bootstrap failed on an empty profiles/api-server/ shell"; }
grep -q 'created api-server' "${tmp}/created" 2>/dev/null \
  || fail "bootstrap did not create profile api-server over an empty shell"

# No directory at all.
fresh_home
run_bootstrap || { cat "${tmp}/out" >&2; fail "bootstrap failed with no profiles/ directory"; }
grep -q 'created api-server' "${tmp}/created" 2>/dev/null \
  || fail "bootstrap did not create profile api-server"

# A directory with a file in it is not ours to remove.
fresh_home
mkdir -p "${tmp}/home/profiles/api-server/memories"
printf 'keep\n' > "${tmp}/home/profiles/api-server/memories/MEMORY.md"
if run_bootstrap; then
  fail "bootstrap succeeded over a non-profile directory that holds files"
fi
[ -f "${tmp}/home/profiles/api-server/memories/MEMORY.md" ] \
  || fail "bootstrap removed a file from a non-profile directory"
grep -q "profiles/api-server" "${tmp}/out" \
  || { cat "${tmp}/out" >&2; fail "bootstrap's refusal does not name the directory"; }

# An existing profile is left alone.
fresh_home
mkdir -p "${tmp}/home/profiles/api-server/memories"
touch "${tmp}/home/profiles/api-server/.identity"
run_bootstrap || { cat "${tmp}/out" >&2; fail "bootstrap failed on an existing profile"; }
[ ! -f "${tmp}/created" ] || fail "bootstrap re-created an existing profile"
[ -d "${tmp}/home/profiles/api-server/memories" ] || fail "bootstrap removed an existing profile's memories/"

echo "ok host part"

# --- image part: the real profile create over a cleared shell ---
hermes_image_gate "${1:-}" "fresh install profiles"

shell_fn="$(fn_source "$bootstrap" clear_empty_profile_shell)" || exit 1
mkdir -p "${tmp}/img"
printf '%s\n' "$shell_fn" > "${tmp}/img/fn.sh"
cat > "${tmp}/img/run.sh" <<'EOF'
set -u
for f in /opt/hermes/cli-config.yaml.example /opt/hermes/.env.example /opt/hermes/docker/SOUL.md; do
  [ -f "$f" ] || { echo "FAIL first-boot seed $f missing from the image; the bootstrap seeds from it"; exit 1; }
done
. /t/fn.sh
export HERMES_HOME=/t/home
mkdir -p "$HERMES_HOME/memories"
printf 'model:\n  default: test\n' > "$HERMES_HOME/config.yaml"

# Upstream behaviour this fix depends on; re-check on Hermes bumps.
mkdir -p "$HERMES_HOME/profiles/api-server/memories"
if hermes profile create api-server --clone >/t/refused.log 2>&1; then
  echo "UPSTREAM: create over an empty shell succeeded; the cleanup may be unneeded"
else
  echo "UPSTREAM: create over an empty shell refused"
fi

clear_empty_profile_shell "$HERMES_HOME/profiles/api-server" || { echo "FAIL cleanup refused an empty shell"; exit 1; }
hermes profile create api-server --clone >/t/create.log 2>&1 || { cat /t/create.log; echo "FAIL create after cleanup"; exit 1; }
hermes profile list 2>/dev/null | grep -q api-server || { echo "FAIL api-server not listed after create"; exit 1; }
echo "image ok"
EOF
out="$(docker run --rm --network none --entrypoint sh -v "${tmp}/img:/t" "$HERMES_IMAGE" /t/run.sh 2>&1)" \
  || { printf '%s\n' "$out" >&2; fail "image part"; }
printf '%s\n' "$out" | grep -q '^image ok' || { printf '%s\n' "$out" >&2; fail "image part"; }
printf '%s\n' "$out" | grep '^UPSTREAM' || true
echo "ok"
