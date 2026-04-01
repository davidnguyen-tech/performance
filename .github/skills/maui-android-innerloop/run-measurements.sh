#!/usr/bin/env bash
# run-measurements.sh — Automate MAUI Android Inner Loop measurements locally.
#
# This script orchestrates the full measurement workflow:
#   1. Bootstrap: install .NET SDK, maui-android workload, build Startup tool
#   2. Create app: generate MAUI template, fix csproj, prepare edit files
#   3. Measure: run test.py androidinnerloop for each runtime configuration
#   4. Cleanup: uninstall APK, remove generated directories
#
# Usage:
#   ./run-measurements.sh --repo-root /path/to/dotnet-performance [OPTIONS]
#
# See --help for full option list.
set -euo pipefail

# ===== Constants =====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXENAME="MauiAndroidInnerLoop"
PACKAGE_NAME="com.companyname.mauiandroidinnerloop"

# ===== Defaults =====
REPO_ROOT=""
CONFIGS="mono-interpreter"
OUTPUT_DIR="./maui-innerloop-results"
ITERATIONS=10
FRAMEWORK="net11.0"
RUNTIME_PACK_PATH=""
RUNTIME_PACK_VERSION="99.99.99-dev"
ANDROID_PACK_PATH=""
ANDROID_PACK_VERSION="99.99.99-dev"
MAUI_VERSION=""
NUGET_FEED=""
SKIP_BOOTSTRAP="false"
SKIP_CREATE_APP="false"
DEVICE=""
DRY_RUN="false"

# ===== Helpers =====
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { log "ERROR: $*" >&2; exit 1; }
warn() { log "WARNING: $*" >&2; }

run_cmd() {
    # Execute a command, or print it if --dry-run is set.
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] $*"
    else
        log "Running: $*"
        "$@"
    fi
}

usage() {
    cat <<'EOF'
Usage: run-measurements.sh [OPTIONS]

Automate MAUI Android Inner Loop measurements locally.

Required:
  --repo-root DIR              Path to dotnet/performance repo root

Options:
  --configs CONFIGS            Comma-separated runtime configs to measure
                               Values: mono-interpreter, coreclr-jit
                               (default: mono-interpreter)
  --output-dir DIR             Where to save binlogs and results
                               (default: ./maui-innerloop-results)
  --iterations N               Number of incremental deploy iterations
                               (default: 10)
  --framework TFM              Target framework moniker without platform suffix
                               (default: net11.0)

Custom Pack Overrides:
  --runtime-pack-path DIR      Path to local NETCore.App runtime pack
                               (enables runtime pack override)
  --runtime-pack-version V     Version string for custom runtime pack
                               (default: 99.99.99-dev)
  --android-pack-path DIR      Path to local Microsoft.Android runtime pack
                               (enables Android pack override)
  --android-pack-version V     Version string for custom Android pack
                               (default: 99.99.99-dev)
  --maui-version VER           Custom MAUI package version
                               (requires packages available in NuGet feed)
  --nuget-feed DIR             Path to additional local NuGet feed

Device Selection:
  --device SERIAL              Target a specific device/emulator by serial
                               (e.g., --device emulator-5554)
                               Run 'adb devices' to list available serials.

Workflow Control:
  --skip-bootstrap             Skip SDK/workload bootstrap
  --skip-create-app            Skip app template creation
  --dry-run                    Print commands without executing
  --help                       Show this usage message

Configuration Mapping:
  mono-interpreter   →  /p:UseMonoRuntime=true
  coreclr-jit        →  /p:UseMonoRuntime=false /p:PublishReadyToRun=false
                        /p:PublishReadyToRunComposite=false

Examples:
  # Basic mono-interpreter measurement
  ./run-measurements.sh --repo-root ~/repos/performance

  # Both configs, 5 iterations
  ./run-measurements.sh --repo-root ~/repos/performance \
    --configs mono-interpreter,coreclr-jit --iterations 5

  # With custom runtime pack from local dotnet/runtime build
  ./run-measurements.sh --repo-root ~/repos/performance \
    --runtime-pack-path ~/repos/runtime/artifacts/bin/microsoft.netcore.app.runtime.android-arm64/Release/
EOF
    exit 0
}

# ===== Argument Parsing =====
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --repo-root)          REPO_ROOT="$2";             shift 2 ;;
            --configs)            CONFIGS="$2";               shift 2 ;;
            --output-dir)         OUTPUT_DIR="$2";            shift 2 ;;
            --iterations)         ITERATIONS="$2";            shift 2 ;;
            --framework)          FRAMEWORK="$2";             shift 2 ;;
            --runtime-pack-path)  RUNTIME_PACK_PATH="$2";     shift 2 ;;
            --runtime-pack-version) RUNTIME_PACK_VERSION="$2"; shift 2 ;;
            --android-pack-path)  ANDROID_PACK_PATH="$2";     shift 2 ;;
            --android-pack-version) ANDROID_PACK_VERSION="$2"; shift 2 ;;
            --maui-version)       MAUI_VERSION="$2";          shift 2 ;;
            --nuget-feed)         NUGET_FEED="$2";            shift 2 ;;
            --device)             DEVICE="$2";               shift 2 ;;
            --skip-bootstrap)     SKIP_BOOTSTRAP="true";      shift   ;;
            --skip-create-app)    SKIP_CREATE_APP="true";     shift   ;;
            --dry-run)            DRY_RUN="true";             shift   ;;
            --help|-h)            usage                                ;;
            *)                    die "Unknown option: $1"             ;;
        esac
    done

    [[ -n "$REPO_ROOT" ]] || die "--repo-root is required"
    REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"  # resolve to absolute path
}

# ===== Prerequisite Validation =====
validate_prereqs() {
    log "Validating prerequisites..."

    # Repo root must contain src/scenarios
    [[ -d "$REPO_ROOT/src/scenarios" ]] \
        || die "Not a dotnet/performance repo: $REPO_ROOT (missing src/scenarios/)"

    # Scenario directory must exist
    [[ -d "$REPO_ROOT/src/scenarios/mauiandroidinnerloop" ]] \
        || die "Missing scenario directory: $REPO_ROOT/src/scenarios/mauiandroidinnerloop/"

    # python3 must be available
    command -v python3 &>/dev/null \
        || die "python3 is required but not found in PATH"

    # adb must be available and a device connected
    command -v adb &>/dev/null \
        || die "adb is required but not found in PATH"

    local device_count
    device_count=$(adb devices | grep -c -E '\t(device|emulator)' || true)
    if [[ "$device_count" -eq 0 ]]; then
        warn "No Android device/emulator detected. Measurements will fail at deploy time."
        warn "Connect a device or start an emulator before running measurements."
    elif [[ "$device_count" -gt 1 ]] && [[ -z "$DEVICE" ]]; then
        log "Multiple devices/emulators detected:"
        adb devices
        die "Use --device SERIAL to select one (e.g., --device emulator-5554)."
    fi

    # Validate config names
    for config in ${CONFIGS//,/ }; do
        case "$config" in
            mono-interpreter|coreclr-jit) ;;
            *) die "Unknown config: $config (valid: mono-interpreter, coreclr-jit)" ;;
        esac
    done

    # Output dir
    mkdir -p "$OUTPUT_DIR"
    OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

    log "  Repo root:  $REPO_ROOT"
    log "  Configs:    $CONFIGS"
    log "  Output dir: $OUTPUT_DIR"
    log "  Iterations: $ITERATIONS"
    log "  Framework:  $FRAMEWORK"
    [[ -n "$RUNTIME_PACK_PATH" ]] && log "  Runtime pack override: $RUNTIME_PACK_PATH (v$RUNTIME_PACK_VERSION)"
    [[ -n "$ANDROID_PACK_PATH" ]] && log "  Android pack override: $ANDROID_PACK_PATH (v$ANDROID_PACK_VERSION)"
    [[ -n "$MAUI_VERSION" ]]      && log "  MAUI version override: $MAUI_VERSION"
    [[ -n "$NUGET_FEED" ]]        && log "  Additional NuGet feed: $NUGET_FEED"
    log "Prerequisites OK."
}

# ===== Bootstrap SDK + Workload + Startup Tool =====
bootstrap_sdk() {
    log "=== Bootstrapping SDK and tools ==="

    local scenarios_dir="$REPO_ROOT/src/scenarios"

    # Source init.sh to download SDK and set PYTHONPATH.
    # init.sh with -channel downloads the SDK to tools/dotnet/<arch>/
    log "Sourcing init.sh -channel main ..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] cd $scenarios_dir && source init.sh -channel main"
    else
        # shellcheck disable=SC1091
        # Temporarily disable 'set -u' (nounset) because init.sh may reference
        # variables like PYTHONPATH that are not yet defined.
        set +u
        (cd "$scenarios_dir" && source ./init.sh -channel main)
        set -u

        # Re-source in current shell to get env vars
        local arch
        arch=$(uname -m)
        case "$arch" in
            x86_64)       arch="x64" ;;
            arm64|aarch64) arch="arm64" ;;
            *)            die "Unsupported architecture: $arch" ;;
        esac

        local dotnet_dir="$REPO_ROOT/tools/dotnet/$arch"
        if [[ -d "$dotnet_dir" ]]; then
            export DOTNET_ROOT="$dotnet_dir"
            export PATH="$dotnet_dir:$PATH"
            export DOTNET_CLI_TELEMETRY_OPTOUT=1
            export DOTNET_MULTILEVEL_LOOKUP=0
            export UseSharedCompilation=false
            log "DOTNET_ROOT=$DOTNET_ROOT"
        else
            die "SDK directory not found after init.sh: $dotnet_dir"
        fi

        # Set PYTHONPATH as init.sh does
        local script_path="$REPO_ROOT/scripts"
        export PYTHONPATH="${PYTHONPATH:-}:$script_path:$scenarios_dir"
    fi

    # Enable lab environment so that the Startup tool (and other perf
    # infrastructure) writes JSON reports.  Without this, even with
    # --report-json-path the report is silently skipped — the C# Reporter
    # class gates on PERFLAB_INLAB=1 before calling File.WriteAllText().
    export PERFLAB_INLAB=1
    log "PERFLAB_INLAB=$PERFLAB_INLAB"

    # Reporter.ParseBuildInfo() reads these env vars.  PERFLAB_BUILDTIMESTAMP
    # is mandatory — DateTime.Parse(null) throws ArgumentNullException.  The
    # rest default to null/empty but we set sensible local values anyway.
    export PERFLAB_BUILDTIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%S.0000000Z)"
    export PERFLAB_REPO="local"
    export PERFLAB_BRANCH="local"
    export PERFLAB_BUILDARCH="$(uname -m)"
    export PERFLAB_LOCALE="en-US"
    export PERFLAB_HASH="local"
    export PERFLAB_BUILDNUM="local-$(date -u +%Y%m%d%H%M%S)"
    log "PERFLAB_BUILDTIMESTAMP=$PERFLAB_BUILDTIMESTAMP"

    # Note: workload installation is handled by pre.py in create_app().
    # pre.py queries NuGet feeds, generates rollback_maui.json, and runs
    # `dotnet workload install maui-android --from-rollback-file`.
    log "Workload installation deferred to pre.py (runs during app creation)."

    # Build the Startup tool (binlog parser) targeting the same TFM as the SDK
    log "Building Startup tool with PERFLAB_TARGET_FRAMEWORKS=$FRAMEWORK ..."
    local startup_csproj="$REPO_ROOT/src/tools/ScenarioMeasurement/Startup/Startup.csproj"
    local startup_output="$REPO_ROOT/artifacts/startup"

    if [[ -f "$startup_output/Startup" ]] || [[ -f "$startup_output/Startup.dll" ]]; then
        log "Startup tool already built at $startup_output — skipping."
    else
        run_cmd env PERFLAB_TARGET_FRAMEWORKS="$FRAMEWORK" \
            dotnet publish "$startup_csproj" \
            -c Release -o "$startup_output" \
            --ignore-failed-sources /p:NuGetAudit=false
    fi

    log "=== Bootstrap complete ==="
}

# ===== Create MAUI App Template =====
create_app() {
    log "=== Creating MAUI app template ==="

    local scenario_dir="$REPO_ROOT/src/scenarios/mauiandroidinnerloop"
    local app_dir="$scenario_dir/app"

    # If app already exists and we're not skipping, clean it first
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would clean app/, traces/, bin/, obj/ directories in $scenario_dir"
    else
        if [[ -d "$app_dir" ]]; then
            log "Removing existing app directory..."
            rm -rf "$app_dir"
        fi

        # Clean traces and bin/obj from previous runs
        rm -rf "$scenario_dir/traces" "$scenario_dir/bin" "$scenario_dir/obj"
    fi

    # Ensure PYTHONPATH is set for the python scripts
    ensure_pythonpath

    # Run pre.py to create template and prepare edit files.
    # pre.py handles workload installation: it queries NuGet feeds for the
    # correct manifest version, generates rollback_maui.json, and runs
    # `dotnet workload install maui-android --from-rollback-file`.
    # Do NOT pass --has-workload — that flag skips installation entirely.
    log "Running pre.py to create template and install workload..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] cd $scenario_dir && python3 pre.py publish -f ${FRAMEWORK}-android"
    else
        (cd "$scenario_dir" && python3 pre.py publish -f "${FRAMEWORK}-android")
    fi

    # Fix csproj: remove non-Android TargetFrameworks
    fix_csproj "$app_dir"

    # Inject AndroidOverridePacks.targets import if custom packs are used
    inject_override_targets "$app_dir"

    # Add local NuGet feed if specified
    inject_nuget_feed "$app_dir"

    log "=== App template created ==="
}

# ===== Fix csproj — restrict to Android-only =====
fix_csproj() {
    local app_dir="$1"
    local csproj="$app_dir/$EXENAME.csproj"

    log "Fixing csproj to remove non-Android TargetFrameworks..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would remove iOS/MacCatalyst/Windows TargetFrameworks from $csproj"
        return
    fi

    [[ -f "$csproj" ]] || die "Cannot find csproj: $csproj"

    # Remove conditional TargetFrameworks lines for iOS/MacCatalyst and Windows.
    # The MAUI template generates lines like:
    #   <TargetFrameworks Condition="!$([MSBuild]::IsOSPlatform('linux'))">$(TargetFrameworks);net11.0-ios;net11.0-maccatalyst</TargetFrameworks>
    #   <TargetFrameworks Condition="$([MSBuild]::IsOSPlatform('windows'))">$(TargetFrameworks);net11.0-windows10.0.19041.0</TargetFrameworks>
    # BSD sed (macOS) requires -i '' for in-place editing.
    sed -i '' '/<TargetFrameworks.*IsOSPlatform/d' "$csproj"

    log "Fixed csproj: $(grep '<TargetFrameworks' "$csproj" | head -1 | xargs)"
}

# ===== Inject AndroidOverridePacks.targets import =====
inject_override_targets() {
    local app_dir="$1"
    local csproj="$app_dir/$EXENAME.csproj"

    # Only inject if any custom pack override is configured
    if [[ -z "$RUNTIME_PACK_PATH" && -z "$ANDROID_PACK_PATH" && -z "$MAUI_VERSION" ]]; then
        return
    fi

    local targets_file="$REPO_ROOT/src/scenarios/build-common/AndroidOverridePacks.targets"

    log "Injecting AndroidOverridePacks.targets import into csproj..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would add <Import Project=\"$targets_file\" /> to $csproj"
        log "[DRY-RUN] Would set isolated NuGet cache at $OUTPUT_DIR/.nuget-packages"
        return 0
    fi

    [[ -f "$targets_file" ]] || die "AndroidOverridePacks.targets not found: $targets_file"

    # Insert the Import right before </Project>
    sed -i '' "s|</Project>|  <Import Project=\"$targets_file\" />\n</Project>|" "$csproj"
    log "Injected override targets import."

    # When using custom packs, isolate NuGet cache to avoid poisoning the global cache
    export NUGET_PACKAGES="$OUTPUT_DIR/.nuget-packages"
    mkdir -p "$NUGET_PACKAGES"
    log "Using isolated NuGet cache: $NUGET_PACKAGES"
}

# ===== Inject additional NuGet feed =====
inject_nuget_feed() {
    local app_dir="$1"

    [[ -n "$NUGET_FEED" ]] || return 0

    local nuget_config="$app_dir/NuGet.config"

    log "Adding local NuGet feed to $nuget_config ..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would add <add key=\"local-override\" value=\"$NUGET_FEED\" /> to $nuget_config"
        return 0
    fi

    if [[ ! -f "$nuget_config" ]]; then
        warn "No NuGet.config found in $app_dir — cannot inject feed."
        return 0
    fi

    # Insert the feed source before the closing </packageSources> tag
    sed -i '' "s|</packageSources>|    <add key=\"local-override\" value=\"$NUGET_FEED\" />\n  </packageSources>|" "$nuget_config"
    log "Added local NuGet feed: $NUGET_FEED"
}

# ===== Get MSBuild Args for a Configuration =====
get_msbuild_args() {
    local config="$1"
    local args=""

    case "$config" in
        mono-interpreter)
            args="/p:UseMonoRuntime=true"
            ;;
        coreclr-jit)
            args="/p:UseMonoRuntime=false;/p:PublishReadyToRun=false;/p:PublishReadyToRunComposite=false"
            ;;
        *)
            die "Unknown config: $config"
            ;;
    esac

    # Append custom pack override properties
    if [[ -n "$RUNTIME_PACK_PATH" ]]; then
        args="${args};/p:CustomNetCoreAppRuntimePackDir=$RUNTIME_PACK_PATH"
        args="${args};/p:CustomNetCoreAppRuntimePackVersion=$RUNTIME_PACK_VERSION"
    fi

    if [[ -n "$ANDROID_PACK_PATH" ]]; then
        args="${args};/p:CustomAndroidRuntimePackDir=$ANDROID_PACK_PATH"
        args="${args};/p:CustomAndroidRuntimePackVersion=$ANDROID_PACK_VERSION"
    fi

    if [[ -n "$MAUI_VERSION" ]]; then
        args="${args};/p:CustomMauiVersion=$MAUI_VERSION"
    fi

    # Pass device serial to MSBuild so -t:Install targets the correct device.
    # The Android SDK expects AdbTarget to contain the full adb flag: "-s <serial>"
    if [[ -n "$DEVICE" ]]; then
        args="${args};/p:AdbTarget=-s $DEVICE"
    fi

    echo "$args"
}

# ===== Detect Edit File Paths =====
# The MAUI template may place MainPage.xaml.cs in different locations
# depending on the template version:
#   - app/Pages/MainPage.xaml.cs  (with -sc / sample content)
#   - app/MainPage.xaml.cs        (plain template)
# Detect dynamically.
detect_edit_paths() {
    local scenario_dir="$REPO_ROOT/src/scenarios/mauiandroidinnerloop"
    local app_dir="$scenario_dir/app"
    local src_dir="$scenario_dir/src"

    if [[ "$DRY_RUN" == "true" ]]; then
        # Use the most common layout as placeholder
        EDIT_SRC="src/MainPage.xaml.cs;src/MainPage.xaml"
        EDIT_DEST="app/Pages/MainPage.xaml.cs;app/Pages/MainPage.xaml"
        log "[DRY-RUN] Using default edit paths (actual paths detected at runtime)"
        return
    fi

    # Find MainPage.xaml.cs in the app directory
    local cs_dest
    cs_dest=$(find "$app_dir" -name "MainPage.xaml.cs" -type f | head -1)
    [[ -n "$cs_dest" ]] || die "Cannot find MainPage.xaml.cs in $app_dir"

    # Find MainPage.xaml in the app directory (same directory as .cs file)
    local xaml_dir
    xaml_dir=$(dirname "$cs_dest")
    local xaml_dest="$xaml_dir/MainPage.xaml"
    [[ -f "$xaml_dest" ]] || die "Cannot find MainPage.xaml at $xaml_dest"

    # Source files (modified versions created by pre.py) are in src/
    local cs_src="$src_dir/MainPage.xaml.cs"
    local xaml_src="$src_dir/MainPage.xaml"

    [[ -f "$cs_src" ]]   || die "Missing edit source: $cs_src (did pre.py run?)"
    [[ -f "$xaml_src" ]]  || die "Missing edit source: $xaml_src (did pre.py run?)"

    # Convert to paths relative to the scenario directory
    local rel_cs_dest="${cs_dest#"$scenario_dir/"}"
    local rel_xaml_dest="${xaml_dest#"$scenario_dir/"}"
    local rel_cs_src="${cs_src#"$scenario_dir/"}"
    local rel_xaml_src="${xaml_src#"$scenario_dir/"}"

    # Return semicolon-separated paths (matching test.py argument format)
    EDIT_SRC="${rel_cs_src};${rel_xaml_src}"
    EDIT_DEST="${rel_cs_dest};${rel_xaml_dest}"

    log "Edit sources:      $EDIT_SRC"
    log "Edit destinations: $EDIT_DEST"
}

# ===== Ensure PYTHONPATH =====
ensure_pythonpath() {
    local scenarios_dir="$REPO_ROOT/src/scenarios"
    local scripts_dir="$REPO_ROOT/scripts"

    if [[ -z "${PYTHONPATH:-}" ]] || [[ "$PYTHONPATH" != *"$scenarios_dir"* ]]; then
        export PYTHONPATH="${PYTHONPATH:-}:$scripts_dir:$scenarios_dir"
        log "Set PYTHONPATH=$PYTHONPATH"
    fi
}

# ===== Ensure DOTNET_ROOT =====
ensure_dotnet_root() {
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would ensure DOTNET_ROOT is set"
        return 0
    fi

    if [[ -n "${DOTNET_ROOT:-}" ]] && [[ -x "$DOTNET_ROOT/dotnet" ]]; then
        return
    fi

    # Try to find the SDK installed by init.sh
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)       arch="x64" ;;
        arm64|aarch64) arch="arm64" ;;
        *)            die "Unsupported architecture: $arch" ;;
    esac

    local dotnet_dir="$REPO_ROOT/tools/dotnet/$arch"
    if [[ -d "$dotnet_dir" ]]; then
        export DOTNET_ROOT="$dotnet_dir"
        export PATH="$dotnet_dir:$PATH"
        export DOTNET_CLI_TELEMETRY_OPTOUT=1
        export DOTNET_MULTILEVEL_LOOKUP=0
        export UseSharedCompilation=false
        log "DOTNET_ROOT=$DOTNET_ROOT"
    else
        die "Cannot find SDK. Run without --skip-bootstrap or set DOTNET_ROOT."
    fi

    # Ensure lab environment flag is set (may already be exported by
    # bootstrap_sdk, but set it here too for --skip-bootstrap paths).
    export PERFLAB_INLAB=1

    # Reporter env vars — see bootstrap_sdk() for detailed comments.
    export PERFLAB_BUILDTIMESTAMP="${PERFLAB_BUILDTIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%S.0000000Z)}"
    export PERFLAB_REPO="${PERFLAB_REPO:-local}"
    export PERFLAB_BRANCH="${PERFLAB_BRANCH:-local}"
    export PERFLAB_BUILDARCH="${PERFLAB_BUILDARCH:-$(uname -m)}"
    export PERFLAB_LOCALE="${PERFLAB_LOCALE:-en-US}"
    export PERFLAB_HASH="${PERFLAB_HASH:-local}"
    export PERFLAB_BUILDNUM="${PERFLAB_BUILDNUM:-local-$(date -u +%Y%m%d%H%M%S)}"
}

# ===== Run Measurement for One Configuration =====
run_measurement() {
    local config="$1"
    local scenario_dir="$REPO_ROOT/src/scenarios/mauiandroidinnerloop"
    local app_dir="$scenario_dir/app"
    local csproj_rel="app/$EXENAME.csproj"

    log "--- Running measurement: $config ---"

    ensure_pythonpath
    ensure_dotnet_root

    # Detect edit file paths dynamically
    detect_edit_paths

    # Build MSBuild args for this config
    local msbuild_args
    msbuild_args=$(get_msbuild_args "$config")
    log "MSBuild args: $msbuild_args"

    # Build the test.py command
    local cmd=(
        python3 test.py androidinnerloop
        --csproj-path "$csproj_rel"
        --edit-src "$EDIT_SRC"
        --edit-dest "$EDIT_DEST"
        --package-name "$PACKAGE_NAME"
        -f "${FRAMEWORK}-android"
        -c Debug
        --msbuild-args "$msbuild_args"
        --inner-loop-iterations "$ITERATIONS"
    )

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] cd $scenario_dir && ${cmd[*]}"
    else
        log "Executing: ${cmd[*]}"
        (cd "$scenario_dir" && "${cmd[@]}")
    fi
}

# ===== Save Binlogs and Results =====
save_binlogs() {
    local config="$1"
    local scenario_dir="$REPO_ROOT/src/scenarios/mauiandroidinnerloop"
    local traces_dir="$scenario_dir/traces"
    local dest_dir="$OUTPUT_DIR/$config"

    log "Saving results for $config to $dest_dir ..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] Would copy $traces_dir/* to $dest_dir/"
        return
    fi

    mkdir -p "$dest_dir"

    if [[ -d "$traces_dir" ]]; then
        cp -r "$traces_dir/"* "$dest_dir/" 2>/dev/null || true
        log "Copied traces to $dest_dir/"

        # List what we saved
        local count
        count=$(find "$dest_dir" -type f | wc -l | tr -d ' ')
        log "  Saved $count files (binlogs + reports)"
    else
        warn "No traces directory found at $traces_dir"
    fi
}

# ===== Cleanup =====
cleanup() {
    local scenario_dir="$REPO_ROOT/src/scenarios/mauiandroidinnerloop"

    log "Running cleanup..."

    ensure_pythonpath
    ensure_dotnet_root

    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY-RUN] cd $scenario_dir && python3 post.py"
        return
    fi

    (cd "$scenario_dir" && python3 post.py) || warn "post.py cleanup failed (non-fatal)"
}

# ===== Main =====
main() {
    parse_args "$@"

    # If --device was specified, export ANDROID_SERIAL so all adb commands target it
    if [[ -n "$DEVICE" ]]; then
        export ANDROID_SERIAL="$DEVICE"
        log "Targeting device: $DEVICE"
    fi

    validate_prereqs

    log "=========================================="
    log " MAUI Android Inner Loop Measurements"
    log "=========================================="

    # Bootstrap: SDK + workload + Startup tool
    if [[ "$SKIP_BOOTSTRAP" != "true" ]]; then
        bootstrap_sdk
    else
        log "Skipping bootstrap (--skip-bootstrap)"
        ensure_dotnet_root
    fi

    # Iterate over each configuration
    for config in ${CONFIGS//,/ }; do
        log ""
        log "=========================================="
        log " Measuring: $config"
        log "=========================================="

        # Create app template (unless skipped)
        if [[ "$SKIP_CREATE_APP" != "true" ]]; then
            create_app
        else
            log "Skipping app creation (--skip-create-app)"
        fi

        # Run the measurement
        run_measurement "$config"

        # Save binlogs to output directory
        save_binlogs "$config"

        # Cleanup between configs (post.py deletes app/ directory)
        cleanup

        log "--- $config measurement complete ---"
    done

    log ""
    log "=========================================="
    log " All measurements complete"
    log "=========================================="
    log "Results saved to: $OUTPUT_DIR"
    log ""

    # Print a summary of saved files per config
    for config in ${CONFIGS//,/ }; do
        local config_dir="$OUTPUT_DIR/$config"
        if [[ -d "$config_dir" ]]; then
            local count
            count=$(find "$config_dir" -type f -name "*.binlog" | wc -l | tr -d ' ')
            local reports
            reports=$(find "$config_dir" -type f -name "*report*.json" | wc -l | tr -d ' ')
            log "  $config: $count binlogs, $reports reports"
        fi
    done
}

main "$@"
