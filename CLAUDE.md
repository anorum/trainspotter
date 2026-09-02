# PDX Train (code namespace: `blockade`)

Read [README.md](README.md) for what the project is and [docs/architecture.md](docs/architecture.md) for what each piece owns and the contracts between them. Where they disagree with older docs, architecture.md wins.

## Commands

```sh
uv sync --all-packages        # every workspace member plus the dev group
uv run ruff check .           # lint (CI runs this)
uv run ruff format --check .  # format check (CI runs this too)
uv run pytest -q              # tests; history-store tests need BLOCKADE_TEST_DATABASE_URL
scripts/dev-web.sh            # hot-reloading web board at localhost:4321, read-only against the live API
```

The web board lives in `services/api/web` (Astro + Preact + TypeScript, `npm test` runs vitest). The iPhone and Watch app lives in `apps/ios`.

## Agent skills

The skills under `.claude/skills/` are copies from [mattpocock/skills](https://github.com/mattpocock/skills) and [poteto/noodle](https://github.com/poteto/noodle) / [poteto/how](https://github.com/poteto/how), pinned in `skills-lock.json`. Update them with `npx skills update`. Run `/ask-matt` to find the right one.

### Issue tracker

Issues live in this repo's GitHub Issues, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels, unchanged: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root (created lazily by `/grill-with-docs`) plus `docs/adr/`. See `docs/agents/domain.md`.
