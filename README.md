# poormans_whatsapp

## Requirements

- Python 3.12+
- uv (https://astral.sh/uv)

---

## Setup

Clone the repo:

```bash
git clone https://github.com/Afraaaaaim/poormans_whatsapp.git
cd poormans_whatsapp
```

Install dependencies:

```bash
uv sync
```

---

## Environment

Copy example env file:

```bash
cp .env.example .env
```

Fill required values in `.env`.

---

## Run

```bash
uv run main.py
```



## Development

Format:

```bash
uv run ruff check .
uv run black .
uv run isort .
```

---

## Adding Dependencies

```bash
uv add <package>
```

Then commit:

- `pyproject.toml`
- `uv.lock`

---

## Versioning

Check version:

```bash
uv version
```

Bump:

```bash
uv version --bump patch
uv version --bump minor
uv version --bump major
```

Commit version change before tagging.

---

# Git Workflow

## First Release (one-time setup)

If no tags exist yet:

```bash
git add .
git commit -m "feat: initial project setup"
git tag v0.1.0
git push origin main --tags
````

---

## Conventional Commits

Use structured commit messages:

```bash
git commit -m "feat: add webhook handler"
git commit -m "fix: handle invalid token"
git commit -m "refactor: simplify service layer"
```

### Types:

Core types (used for version bumping):

* feat → new feature (minor)

* fix → bug fix (patch)

* BREAKING CHANGE: in footer → major

### Common additional types (no automatic bump unless configured):

* refactor → code change without feature/fix

* perf → performance improvement

* docs → documentation only

* test → tests added/changed

* chore → tooling/config/deps

* build → build system changes

* ci → CI/CD changes

* style → formatting, no logic change

* revert → revert previous commit

---

## Bump Version + Update Changelog

```bash
uv run cz bump
git push origin main --tags
```


## License

MIT
