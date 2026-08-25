# Release Checklist

## For each release

1. Update `project.version` in [`pyproject.toml`](./pyproject.toml), then run
   `uv lock`.
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

Run the permissioned integration tests on macOS 26. Grant System Audio
Recording, Microphone, and Automation access to the test runner. Close
QuickTime Player, keep the default output audible, and pause other audio.

```bash
CATAP_RUN_INTEGRATION=1 CATAP_RUN_TONE_INTEGRATION=1 \
  uv run --group dev pytest -m integration
```

Require no skips. The suite plays audible tones, controls QuickTime Player,
and records the default microphone. Hosted CI cannot run this gate.

4. Commit and tag:

```bash
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: release X.Y.Z"
git tag vX.Y.Z
git push origin main vX.Y.Z
```

5. Publish to TestPyPI:

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
  catap==X.Y.Z
catap --help
catap list-apps
```

(The `--extra-index-url` is required because TestPyPI does not mirror
the `pyobjc-*` runtime dependencies.)

7. Create the GitHub Release for `vX.Y.Z`. This runs `publish.yml`.
8. Confirm the workflow succeeds and `catap==X.Y.Z` is on PyPI.

## Optional smoke checks after publish

```bash
uv venv --seed --python 3.12 /tmp/catap-smoke
source /tmp/catap-smoke/bin/activate
pip install catap==X.Y.Z
catap --help
```
