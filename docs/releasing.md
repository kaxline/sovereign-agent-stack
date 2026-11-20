# Releasing

Downstream installs pin a tag, and moving that pin is a migration for each of
them. Every release gets an annotated tag and a [changelog](../CHANGELOG.md)
entry that answers what a downstream maintainer would otherwise have to work
out from a diff.

## Version numbers

`vMAJOR.MINOR.PATCH`. While the stack is `0.x`:

- **Minor** (`v0.3.0`): new services, new required `.env` keys, new host
  requirements, or changed mount targets or ports.
- **Patch** (`v0.2.1`): fixes that need nothing beyond `git checkout`,
  `./scripts/setup.sh` and `docker compose up -d`.

## Changelog entry

Add a section at the top of `CHANGELOG.md` with these headings, leaving out
any that are empty:

1. **Upgrade steps**: commands to run, overrides that can be dropped, images
   that are no longer used.
2. **New services**: name, profile, and what it is for.
3. **Required `.env` keys**: whether `setup.sh` fills them in.
4. **New optional `.env` keys**: with defaults. Defaults must keep the
   previous behaviour.
5. **Host requirements**: new commands `setup.sh` or `make` call on the host.
6. **Changes**: short notes. Call out any change to a stable container path
   listed in [extending.md](extending.md#add-or-change-mounts).

To find what changed since the last tag:

```bash
git log --oneline "$(git describe --tags --abbrev=0)"..HEAD
```

```bash
git diff "$(git describe --tags --abbrev=0)" -- docker-compose.yml docker-compose.hermes-terminal.yml compose/hermes/terminal-ssh.yml .env.example scripts/setup.sh
```

## Upgrade hooks

Every item under **Upgrade steps** is code in `upgrades/vX.Y.Z.sh`, named for
the release that needs it, or is reported there as `needs_user` when only the
user can do it (re-entering a credential, changing an item in their vault). A
release whose upgrade steps are prose only is not tagged. `scripts/upgrade.sh`
runs the hooks an upgrade crosses, so an install that skips releases still runs
each one; hooks must be idempotent. `upgrades/lib.sh` lists the directives a
hook can print (`rebuild`, `reload_hermes`, `needs_user`, `notice`).

Experimental add-ons still get no migration code. When an upgrade leaves one
needing the user's help, its hook says so with `needs_user` instead.

`scripts/test-upgrade-sh.sh` runs the shipped hooks on a synthetic stack; add a
case there for a new hook's directives.

## Check

Run `make bash32-test` on a Mac before tagging. It runs the host scripts under
`/bin/bash` 3.2, which is what installs get without Homebrew's bash. Tests on
a machine with a newer `bash` first on PATH pass scripts that fail there.

CI's shellcheck and hadolint jobs run `scripts/lint.sh`, which pins both
linters as container images. `make test` runs the same script through
`scripts/test-lint.sh` whenever Docker is available, so `land` fails on what
those jobs would fail on. `make lint` runs it alone. To move a linter to a
new version, change its image in `scripts/lint.sh`; CI and local runs follow.

## Tag

After the changelog commit is on `main`:

```bash
git tag -a v0.2.0 -m "v0.2.0"
```

Push the tag only when the release is meant to be public:

```bash
git push origin v0.2.0
```
