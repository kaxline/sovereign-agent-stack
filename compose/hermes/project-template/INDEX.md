# Index

Run from the repo root after you add or change source files or `sources.yaml`:

```bash
make project-index PROJECT=<this-directory-name>
```

Add a `description:` field in each source file's YAML frontmatter so the tables
stay useful without an LLM summarizer. Extra host paths must be mounted with
`make context-root-add` before Hermes can open them.
