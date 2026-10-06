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
git diff "$(git describe --tags --abbrev=0)" -- docker-compose.yml compose/hermes/terminal-ssh.yml .env.example scripts/setup.sh
```

## Tag

After the changelog commit is on `main`:

```bash
git tag -a v0.2.0 -m "v0.2.0"
```

Push the tag only when the release is meant to be public:

```bash
git push origin v0.2.0
```
