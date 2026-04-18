# Release Checklist

## One-time setup

1. Make the repository public, or update the project URLs in [`pyproject.toml`](./pyproject.toml) so PyPI will not point users at a private repo.
2. Register Trusted Publishers for `catap`:
   - On [PyPI](https://pypi.org/manage/account/publishing/): workflow `publish.yml`, environment `pypi`, owner `sbetko`, repo `catap`.
   - On [TestPyPI](https://test.pypi.org/manage/account/publishing/): workflow `publish-test.yml`, environment `testpypi`, owner `sbetko`, repo `catap`.
3. In GitHub, create the `pypi` and `testpypi` environments and grant approval rules as needed.
4. Ensure both workflows (`.github/workflows/publish.yml` and `.github/workflows/publish-test.yml`) are enabled.

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

If the current Mac has already granted system-audio permission to your
terminal app, also run the opt-in integration smoke tests:

```bash
CATAP_RUN_INTEGRATION=1 uv run --group dev pytest -m integration
```

Optional manual GUI smoke test:

```bash
uv run python scripts/catap_demo_gui.py
```

4. Commit and tag:

```bash
git tag vX.Y.Z
git push origin main --tags
```

5. Dry-run on TestPyPI (manual dispatch):

```bash
gh workflow run publish-test.yml --ref vX.Y.Z
gh run watch
```

6. Smoke-test the TestPyPI upload:

```bash
uv venv --seed --python 3.12 /tmp/catap-testpypi
source /tmp/catap-testpypi/bin/activate
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  catap
catap --help
catap list-apps
```

(The `--extra-index-url` is required because TestPyPI does not mirror
the `pyobjc-*` runtime dependencies.)

7. Create a GitHub Release from tag `vX.Y.Z` to trigger `publish.yml` → PyPI.
8. Confirm the `Publish` workflow completes and the package appears on PyPI.

## Optional smoke checks after publish

```bash
uv venv --seed --python 3.12 /tmp/catap-smoke
source /tmp/catap-smoke/bin/activate
pip install catap
catap --help
```
