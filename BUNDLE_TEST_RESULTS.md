# catap Bundle Stub Test Results

## Summary

The `.app` bundle stub has been successfully created and verified. This bundle provides the necessary Info.plist with `NSAudioCaptureUsageDescription` so macOS can properly request audio capture permissions.

## Bundle Structure ✓

```
src/catap/catap.app/
└── Contents/
    ├── Info.plist           # Valid plist with NSAudioCaptureUsageDescription
    ├── PkgInfo              # "APPL????"
    └── MacOS/
        └── catap            # Executable shell script launcher
```

## Validation Results

### ✓ Info.plist Validation
```bash
$ plutil -lint src/catap/catap.app/Contents/Info.plist
src/catap/catap.app/Contents/Info.plist: OK
```

### ✓ Permission Key Present
```bash
$ plutil -p src/catap/catap.app/Contents/Info.plist | grep NSAudioCaptureUsageDescription
"NSAudioCaptureUsageDescription" => "catap needs access to capture audio from other applications."
```

### ✓ Bundle Identifier Configured
```bash
$ plutil -p src/catap/catap.app/Contents/Info.plist | grep CFBundleIdentifier
"CFBundleIdentifier" => "io.github.catap"
```

### ✓ macOS Bundle Recognition
```bash
$ mdls -name kMDItemContentType src/catap/catap.app
kMDItemContentType = "com.apple.application-bundle"
```

macOS correctly recognizes catap.app as a valid application bundle.

### ✓ Launcher Script Functionality
The launcher script successfully:
- Sets the `CATAP_RUNNING_FROM_BUNDLE=1` environment variable
- Finds Python with catap installed
- Executes `python -m catap` with all arguments passed through

Tested with:
```bash
$ PATH="$PWD/.venv/bin:$PATH" src/catap/catap.app/Contents/MacOS/catap test-bundle
Running from bundle: ✓ YES
Environment variable CATAP_RUNNING_FROM_BUNDLE is set
```

## Test Command

A `test-bundle` command has been added to verify the bundle configuration:

```bash
# Direct invocation (shows NOT from bundle)
$ uv run catap test-bundle

# Via launcher script (shows YES from bundle)
$ PATH="$PWD/.venv/bin:$PATH" src/catap/catap.app/Contents/MacOS/catap test-bundle
```

Output includes:
- Bundle path and existence
- Info.plist validation
- Launcher script status
- Bundle environment detection
- Python executable path

## Development vs Production

### Development (Editable Install)
When using `uv pip install -e .`, catap is not in the system Python's site-packages. The launcher needs the venv's Python in PATH to work.

**Testing workaround:**
```bash
PATH="$PWD/.venv/bin:$PATH" open -a src/catap/catap.app --args test-bundle --write-log
```

### Production (Normal Install)
When installed via `pip install catap` or `uv pip install catap`, the package will be in Python's site-packages and the bundle will work correctly with any Python that has catap installed.

## Next Steps

The bundle stub is **ready for Core Audio bindings**. When the actual API calls are implemented:

1. Commands that need audio capture permissions should use `ensure_running_from_bundle()`
2. This will automatically relaunch via the bundle if not already running from it
3. macOS will show the permission dialog using the text from `NSAudioCaptureUsageDescription`
4. Once granted, permissions are stored by bundle ID (`io.github.catap`)

## Files Created

- `src/catap/catap.app/Contents/Info.plist` - Bundle configuration with permission key
- `src/catap/catap.app/Contents/PkgInfo` - Bundle type identifier
- `src/catap/catap.app/Contents/MacOS/catap` - Smart launcher script
- `src/catap/bundle/launcher.py` - Python utilities for bundle detection/relaunch
- `src/catap/cli.py` - Added `test-bundle` command for verification

## Verification Checklist

- [x] Info.plist is syntactically valid
- [x] NSAudioCaptureUsageDescription is present and readable
- [x] CFBundleIdentifier is set correctly
- [x] macOS recognizes it as an application bundle
- [x] Launcher script is executable
- [x] Launcher sets CATAP_RUNNING_FROM_BUNDLE correctly
- [x] Bundle detection utilities work
- [x] Test command verifies all components

**Status: Ready to proceed with Core Audio Tap bindings**
