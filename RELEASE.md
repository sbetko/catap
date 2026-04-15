# Release Checklist

## One-time setup

1. Make the repository public, or update the project URLs in [`pyproject.toml`](./pyproject.toml) so PyPI will not point users at a private repo.
2. Enable Trusted Publishing for `catap` in PyPI and allow GitHub repository `sbetko/catap`.
3. In GitHub, create the `pypi` environment and grant approval rules as needed.
4. Ensure the `Publish` workflow (`.github/workflows/publish.yml`) is enabled.

## For each release

1. Update version in [`pyproject.toml`](./pyproject.toml).
2. Add release notes in [`CHANGELOG.md`](./CHANGELOG.md).
3. Run quality gates locally:

```bash
uv sync --group dev
uv run --group dev ruff check .
uv run --group dev ty check --error-on-warning src tests
uv run --group dev pytest
uv run --group dev python -m build
uv run --group dev twine check dist/*
```

4. Commit and tag:

```bash
git tag vX.Y.Z
git push origin main --tags
```

5. Create a GitHub Release from tag `vX.Y.Z`.
6. Confirm the `Publish` workflow completes and package appears on PyPI.

## Optional smoke checks after publish

```bash
python3 -m venv /tmp/catap-smoke
source /tmp/catap-smoke/bin/activate
pip install catap
catap --help
```
