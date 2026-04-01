---
name: maui-android-innerloop
description: Guide for running MAUI Android Inner Loop deploy measurements in the dotnet/performance repo, including an automation script and custom framework pack support. Use this when asked to measure, benchmark, or compare MAUI Android first deploy and incremental deploy times across runtime configurations (Mono+Interpreter, CoreCLR+JIT, etc.), or when testing local builds of dotnet/runtime, dotnet/android, or dotnet/maui.
---

# MAUI Android Inner Loop Measurement

Measures first deploy and incremental deploy times for a .NET MAUI Android app using MSBuild binary logs. Located in `src/scenarios/mauiandroidinnerloop/` within the dotnet/performance repo.

## Prerequisites

Before running measurements, verify:

1. **Android device** connected via USB: `adb devices` should list a device.
2. **.NET SDK** installed at `tools/dotnet/arm64/`. Bootstrap with:
   ```bash
   cd src/scenarios && . ./init.sh -channel main
   ```
3. **maui-android workload** installed (NOT full `maui` — iOS packages fail without the iOS workload):
   ```bash
   export DOTNET_ROOT="$(pwd)/tools/dotnet/arm64"
   export PATH="$DOTNET_ROOT:$PATH"
   dotnet workload install maui-android \
     --from-rollback-file src/scenarios/mauiandroidinnerloop/rollback_maui.json \
     --skip-sign-check
   ```
4. **Startup tool** (binlog parser) built:
   ```bash
   PERFLAB_TARGET_FRAMEWORKS=net11.0 dotnet publish \
     src/tools/ScenarioMeasurement/Startup/Startup.csproj \
     -c Release -o artifacts/startup --ignore-failed-sources /p:NuGetAudit=false
   ```

## Quick Start (Automated)

The `run-measurements.sh` script in this skill directory automates the entire workflow — bootstrap, app creation, csproj fixing, measurement, binlog collection, and cleanup.

```bash
# Quick: measure Mono+Interpreter (default)
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs mono-interpreter

# Both configs
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs mono-interpreter,coreclr-jit \
  --output-dir ./results

# Dry run to see what would happen
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --dry-run
```

The script handles all the gotchas documented in the manual workflow below (csproj fixing, edit file detection, cleanup between configs, etc.). Use `--skip-bootstrap` to reuse an already-installed SDK, or `--skip-create-app` to reuse an existing app template.

For custom framework pack overrides (local dotnet/runtime, dotnet/android, or dotnet/maui builds), see [Using Custom Framework Packs](#using-custom-framework-packs) below.

## Environment Setup (every new shell)

```bash
cd src/scenarios && . ./init.sh -dotnetdir <REPO_ROOT>/tools/dotnet/arm64
cd mauiandroidinnerloop
```

## Create the App Template

```bash
python3 pre.py publish -f net11.0-android --has-workload
```

### CRITICAL: Fix csproj after pre.py

`dotnet new maui` targets all platforms. Since only maui-android workload is installed, you MUST edit `app/MauiAndroidInnerLoop.csproj` and remove the iOS/MacCatalyst/Windows TargetFrameworks conditions, leaving only:

```xml
<TargetFrameworks>net11.0-android</TargetFrameworks>
```

Remove these two lines that follow the android TargetFrameworks line:
```xml
<TargetFrameworks Condition="!$([MSBuild]::IsOSPlatform('linux'))">$(TargetFrameworks);net11.0-ios;net11.0-maccatalyst</TargetFrameworks>
<TargetFrameworks Condition="$([MSBuild]::IsOSPlatform('windows'))">$(TargetFrameworks);net11.0-windows10.0.19041.0</TargetFrameworks>
```

## MSBuild Args per Configuration

| Configuration     | MSBuild Properties                                                                    |
|-------------------|---------------------------------------------------------------------------------------|
| Mono+Interpreter  | `/p:UseMonoRuntime=true`                                                              |
| CoreCLR+JIT       | `/p:UseMonoRuntime=false /p:PublishReadyToRun=false /p:PublishReadyToRunComposite=false` |

## Running a Measurement

```bash
# Clean from any prior run
rm -rf app/bin app/obj traces

# Mono+Interpreter
python3 test.py androidinnerloop \
  --csproj-path app/MauiAndroidInnerLoop.csproj \
  --edit-src src/MainPage.xaml.cs \
  --edit-dest app/MainPage.xaml.cs \
  -f net11.0-android -c Debug \
  --msbuild-args "/p:UseMonoRuntime=true"

# CoreCLR+JIT
python3 test.py androidinnerloop \
  --csproj-path app/MauiAndroidInnerLoop.csproj \
  --edit-src src/MainPage.xaml.cs \
  --edit-dest app/MainPage.xaml.cs \
  -f net11.0-android -c Debug \
  --msbuild-args "/p:UseMonoRuntime=false;/p:PublishReadyToRun=false;/p:PublishReadyToRunComposite=false"
```

## What test.py androidinnerloop Does

1. **First deploy:** `dotnet build <csproj> -t:Install -c Debug -f net11.0-android <msbuild-args> /p:UseSharedCompilation=true -bl:traces/first-deploy.binlog`
2. **File edit:** Copies modified `MainPage.xaml.cs` to simulate an incremental code edit
3. **Incremental deploy:** `dotnet build <csproj> -t:Install ... -bl:traces/incremental-deploy.binlog`
4. **Parse binlogs:** Extracts per-task timings using the Startup tool

## Clean Between Configurations

```bash
python3 post.py   # Uninstalls APK, shuts down build servers, removes app/traces/etc.
python3 pre.py publish -f net11.0-android --has-workload
# Fix csproj again! (remove iOS/MacCatalyst/Windows targets)
```

## Persisting Binlogs

Binlogs are in `traces/` and get cleaned by `post.py`. To keep them:

```bash
mkdir -p binlogs
cp traces/first-deploy.binlog binlogs/<config>-first-deploy.binlog
cp traces/incremental-deploy.binlog binlogs/<config>-incremental-deploy.binlog
```

## Key Facts & Gotchas

- **UseSharedCompilation=true** is set by `runner.py` for this scenario, overriding the repo default of `false`. This mirrors a real dev workflow where the Roslyn compiler server stays warm. `post.py` runs `dotnet build-server shutdown` to clean up between runs.
- **FastDev** (Fast Deployment) is ON by default in Debug (`EmbedAssembliesIntoApk=false`). Do NOT set `EmbedAssembliesIntoApk=true` — it disables FastDev and makes deploys 10x slower.
- **Startup tool** targets `net8.0` by default and needs .NET 8 runtime. Build with `PERFLAB_TARGET_FRAMEWORKS=net11.0` to retarget if only .NET 11 is available.
- **Dead NuGet feeds** (`darc-pub-dotnet-android-*`) break Startup tool builds. Use `--ignore-failed-sources /p:NuGetAudit=false`.
- **macOS Spotlight** can race with builds causing random errors (XARDF7024, MAUIR0001, CS2012). Fix: `sudo mdutil -i off <worktree_path>`.
- **The repo sets `UseSharedCompilation=false`** in `src/Directory.Build.props` and `src/scenarios/init.sh`. The runner.py override via `/p:UseSharedCompilation=true` on the command line takes precedence.

## Using Custom Framework Packs

The automation script supports overriding the runtime packs used during build+deploy, enabling you to measure local builds from dotnet/runtime, dotnet/android, or dotnet/maui without modifying the installed workload.

### Scenario 1: Local dotnet/runtime Build

Test a custom .NET runtime (CoreCLR or Mono) built locally:

```bash
# 1. In dotnet/runtime repo, build for Android:
./build.sh -s clr+libs+packs+host -c Release -os android -a arm64

# 2. Run measurements with custom runtime pack:
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs mono-interpreter \
  --runtime-pack-path /path/to/runtime/artifacts/bin/microsoft.netcore.app.runtime.android-arm64/Release \
  --nuget-feed /path/to/runtime/artifacts/packages/Release/Shipping
```

⚠️ **P/Invoke Version Skew Warning**: Android CoreCLR uses a precompiled P/Invoke table in `libnet-android.release.so`. If the runtime version doesn't match what the Android workload expects, the app may crash with `abort()`. This is less of a concern for Mono+Interpreter, but critical for CoreCLR configurations.

### Scenario 2: Local dotnet/android Build

Test a custom Android runtime pack (e.g., modified Java interop, build tasks):

```bash
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs mono-interpreter \
  --android-pack-path /path/to/android/artifacts/bin/Microsoft.Android.Runtime.Mono/Release/android-arm64
```

### Scenario 3: Local dotnet/maui Build

Test custom MAUI packages (requires the NuGet packages to be available in a feed):

```bash
# Requires MAUI NuGet packages in a feed
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs mono-interpreter \
  --maui-version 11.0.0-dev.123 \
  --nuget-feed /path/to/maui/artifacts/packages/Release/Shipping
```

### Combined: Multiple Local Builds

All three overrides can be used together to test a fully custom stack:

```bash
~/.copilot/skills/maui-android-innerloop/run-measurements.sh \
  --repo-root /path/to/dotnet-performance \
  --configs coreclr-jit \
  --runtime-pack-path /path/to/runtime/artifacts/bin/microsoft.netcore.app.runtime.android-arm64/Release \
  --android-pack-path /path/to/android/artifacts/bin/Microsoft.Android.Runtime.CoreCLR/Release/android-arm64 \
  --maui-version 11.0.0-dev.123 \
  --nuget-feed /path/to/maui/artifacts/packages/Release/Shipping
```

## How Custom Packs Work (MSBuild)

When custom pack overrides are specified, the script:

1. **Injects an `<Import>` into the app's csproj** pointing to `AndroidOverridePacks.targets` (at `src/scenarios/build-common/AndroidOverridePacks.targets`).
2. **Passes properties via MSBuild args** — e.g., `/p:CustomNetCoreAppRuntimePackDir=/path/to/pack`.
3. **The targets override `ResolvedRuntimePack` items** after the SDK's `ProcessFrameworkReferences` and `ResolveFrameworkReferences` targets run, redirecting the build to use the local pack directory instead of downloading from NuGet.
4. **NuGet package cache is isolated** — the script sets `NUGET_PACKAGES` to a local directory under `--output-dir` to avoid poisoning the global NuGet cache with custom packages.

This follows the same pattern as `WasmOverridePacks.targets` used for WebAssembly scenarios in the same repo.

The three MSBuild properties and their effects:

| Property | What it overrides | Activated by |
|----------|-------------------|--------------|
| `CustomNetCoreAppRuntimePackDir` | `Microsoft.NETCore.App.Runtime.android-arm64` pack | `--runtime-pack-path` |
| `CustomAndroidRuntimePackDir` | `Microsoft.Android.Runtime.*` pack | `--android-pack-path` |
| `CustomMauiVersion` | `$(MauiVersion)` for MAUI NuGet packages | `--maui-version` |

## Script Reference

Full CLI reference for `run-measurements.sh`:

| Flag | Description | Default |
|------|-------------|---------|
| `--repo-root DIR` | Path to dotnet/performance repo root | (required) |
| `--configs CONFIGS` | Comma-separated configs: `mono-interpreter`, `coreclr-jit` | `mono-interpreter` |
| `--output-dir DIR` | Results output directory | `./maui-innerloop-results` |
| `--device SERIAL` | Target a specific device/emulator (e.g., `emulator-5554`) | (none — auto-detect) |
| `--iterations N` | Incremental deploy iterations | `10` |
| `--framework TFM` | Target framework (without platform suffix) | `net11.0` |
| `--runtime-pack-path DIR` | Local NETCore.App runtime pack directory | (none) |
| `--runtime-pack-version V` | Custom runtime pack version string | `99.99.99-dev` |
| `--android-pack-path DIR` | Local Microsoft.Android runtime pack directory | (none) |
| `--android-pack-version V` | Custom Android pack version string | `99.99.99-dev` |
| `--maui-version VER` | Custom MAUI package version | (none) |
| `--nuget-feed DIR` | Additional local NuGet feed path | (none) |
| `--skip-bootstrap` | Skip SDK/workload install (reuse existing) | `false` |
| `--skip-create-app` | Skip app template creation (reuse existing) | `false` |
| `--dry-run` | Print commands without executing | `false` |
