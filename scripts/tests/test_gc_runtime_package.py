import hashlib
import io
import base64
import json
import shutil

import pytest

from scripts.gc_runtime_package import (
    EVIDENCE_FILE_NAME,
    EXPECTED_PILOT_TESTS,
    PACKAGE_ID,
    RUNTIME_REPOSITORY,
    main,
    profile_from_perflab_environment,
    validate_benchmark_artifacts,
    validate_downloaded_results,
    fetch_package_evidence,
    validate_package_metadata,
    validate_profile,
)

VERSION = "12.0.0-gcbase.1.26465.1"
COMMIT = "1" * 40
BRANCH = "feature/gc/baseline"
QUEUE = "Ubuntu.2204.Amd64.Viper.Perf"
BUILD_ID = "3079063"
PACKAGE_SHA512 = "2" * 128


def test_exact_profile_retains_candidate_identity():
    profile = validate_profile(VERSION, COMMIT.upper(), BRANCH, QUEUE, BUILD_ID)
    assert profile["GcRuntimePackageVersion"] == VERSION
    assert profile["GcRuntimeCommit"] == COMMIT
    assert profile["GcRuntimeBranch"] == BRANCH
    assert profile["GcPerformanceQueue"] == QUEUE
    assert profile["GcRuntimeBuildId"] == BUILD_ID
    assert profile["GcRuntimePackageId"] == PACKAGE_ID
    assert profile["GcTargetFramework"] == "net11.0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": ""},
        {"version": "12.0.*-gcbase"},
        {"version": VERSION + '"; echo invalid'},
        {"version": "12.0.0-another.1"},
        {"version": "12.0.0-gcbase2.1"},
        {"version": "12.0.0-gcbase-.1"},
        {"commit": "1234"},
        {"commit": COMMIT + "\n"},
        {"branch": "main"},
        {"branch": "feature/gc/../baseline"},
        {"queue": ""},
        {"queue": "Ubuntu.2204.Amd64.Open"},
        {"queue": "Windows.11.Amd64.Viper.Perf"},
        {"queue": QUEUE + ";invalid"},
        {"build_id": ""},
        {"build_id": "build-3079063"},
    ],
)
def test_invalid_profile_fails_before_pipeline_variable_output(overrides):
    arguments = dict(version=VERSION, commit=COMMIT, branch=BRANCH, queue=QUEUE, build_id=BUILD_ID)
    arguments.update(overrides)
    with pytest.raises(ValueError):
        validate_profile(**arguments)


def package_metadata(package_id=PACKAGE_ID, version=VERSION, repository=RUNTIME_REPOSITORY, commit=COMMIT):
    return f"""<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
      <metadata><id>{package_id}</id><version>{version}</version>
        <repository type="git" url="{repository}" commit="{commit}" />
      </metadata>
    </package>"""


def test_package_source_must_match_the_requested_candidate():
    validate_package_metadata(package_metadata(), VERSION, COMMIT)
    for xml in [
        package_metadata(package_id="Microsoft.NETCore.App.Runtime.win-x64"),
        package_metadata(version="12.0.0-other.1"),
        package_metadata(repository="https://github.com/dotnet/runtime"),
        package_metadata(commit="2" * 40),
    ]:
        with pytest.raises(ValueError):
            validate_package_metadata(xml, VERSION, COMMIT)


def test_nuget_namespace_version_does_not_change_package_identity():
    xml = package_metadata().replace("2013/05", "2011/08")
    validate_package_metadata(xml, VERSION, COMMIT)


def test_package_sha512_is_computed_from_the_exact_nupkg(monkeypatch):
    metadata = package_metadata().encode("utf-8")
    package = b"exact nupkg contents"
    responses = iter([io.BytesIO(metadata), io.BytesIO(package)])
    monkeypatch.setattr("scripts.gc_runtime_package._get_package_base_address", lambda: "https://example.test/flat2")
    monkeypatch.setattr("scripts.gc_runtime_package.urlopen", lambda *_args, **_kwargs: next(responses))

    actual_metadata, package_sha512 = fetch_package_evidence(VERSION)

    assert actual_metadata == metadata
    assert package_sha512 == hashlib.sha512(package).hexdigest()


def get_profile():
    profile = validate_profile(VERSION, COMMIT, BRANCH, QUEUE, BUILD_ID)
    profile["GcRuntimePackageSha512"] = PACKAGE_SHA512
    return profile


def test_preflight_reads_pipeline_inputs_and_publishes_validated_values(monkeypatch):
    for name, value in {
        "BUILD_REASON": "Manual",
        "SYSTEM_TEAMPROJECT": "internal",
        "GC_RUNTIME_PACKAGE_VERSION": VERSION,
        "GC_RUNTIME_COMMIT": COMMIT,
        "GC_RUNTIME_BUILD_ID": BUILD_ID,
        "GC_RUNTIME_BRANCH": BRANCH,
        "GC_RUNTIME_PERFORMANCE_QUEUE": QUEUE,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "scripts.gc_runtime_package.fetch_package_evidence",
        lambda _version: (package_metadata().encode("utf-8"), PACKAGE_SHA512),
    )
    variables = {}
    monkeypatch.setattr(
        "performance.common.set_environment_variable",
        lambda name, value: variables.__setitem__(name, value),
    )

    assert main([]) == 0
    assert variables == get_profile()


def test_perflab_environment_reconstructs_validated_profile():
    environment = {
        "PERFLAB_RUNTIME_PACKAGE_VERSION": VERSION,
        "PERFLAB_HASH": COMMIT,
        "PERFLAB_BRANCH": BRANCH,
        "PERFLAB_QUEUE": QUEUE,
        "PERFLAB_DATA_RUNTIME_PACKAGE_BUILD_ID": BUILD_ID,
        "PERFLAB_DATA_RUNTIME_PACKAGE_COMMIT": COMMIT,
        "PERFLAB_DATA_RUNTIME_PACKAGE_FEED": get_profile()["GcRuntimeFeed"],
        "PERFLAB_DATA_RUNTIME_PACKAGE_ID": PACKAGE_ID,
        "PERFLAB_DATA_RUNTIME_PACKAGE_RID": "linux-x64",
        "PERFLAB_DATA_RUNTIME_PACKAGE_SHA512": PACKAGE_SHA512,
    }

    assert profile_from_perflab_environment(environment) == get_profile()


def create_report(test_names):
    return {
        "tests": [
            {
                "categories": ["Runtime"],
                "name": name,
                "additionalData": {},
                "counters": [
                    {
                        "name": "Duration of single invocation",
                        "results": [1.0],
                    }
                ],
            }
            for name in test_names
        ],
        "run": {
            "queue": QUEUE,
            "name": "gc-runtime-package-test",
            "perfRepoHash": "3" * 40,
        },
        "build": {
            "repo": "dotnet/runtimelab",
            "branch": BRANCH,
            "architecture": "x64",
            "gitHash": COMMIT,
            "additionalData": {
                "RUNTIME_PACKAGE_BUILD_ID": BUILD_ID,
                "RUNTIME_PACKAGE_COMMIT": COMMIT,
                "RUNTIME_PACKAGE_FEED": "https://pkgs.dev.azure.com/dnceng/public/_packaging/dotnet-experimental/nuget/v3/index.json",
                "RUNTIME_PACKAGE_ID": PACKAGE_ID,
                "RUNTIME_PACKAGE_RID": "linux-x64",
                "RUNTIME_PACKAGE_SHA512": PACKAGE_SHA512,
                "PERFLAB_RUNTIME_PACKAGE_VERSION": VERSION,
                "targetFrameworks": "net11.0",
                "BenchmarkDotNetVersion": "0.16.0-test",
                "sdkVersion": "11.0.100-test",
            },
        },
        "inLab": True,
    }


def create_artifact_fixture(tmp_path):
    artifacts = tmp_path / "artifacts"
    packages = tmp_path / "packages"
    output = tmp_path / "generated" / "bin" / "Release" / "net11.0" / "linux-x64"
    generated = output.parents[3]
    artifacts.mkdir()
    output.mkdir(parents=True)

    expected_names = sorted(EXPECTED_PILOT_TESTS)
    (artifacts / "Byte-perf-lab-report.json").write_text(
        json.dumps(create_report(expected_names[:2])), encoding="utf-8"
    )
    (artifacts / "Char-perf-lab-report.json").write_text(
        json.dumps(create_report(expected_names[2:])), encoding="utf-8"
    )
    log_lines = []
    for name in expected_names:
        log_lines.extend(
            [
                f'// Execute: ./benchmark --benchmarkName "{name}" in {output}',
                "// Runtime=.NET 12.0.0 (12.0.0-gcbase.1.26465.1, test)",
                "// GC=Concurrent Workstation",
            ]
        )
    (artifacts / "BenchmarkRun-test.log").write_text("\n".join(log_lines), encoding="utf-8")

    (generated / "BenchmarkDotNet.Autogenerated.csproj").write_text(
        "<Project><PropertyGroup>"
        "<ServerGarbageCollection>false</ServerGarbageCollection>"
        "<ConcurrentGarbageCollection>true</ConcurrentGarbageCollection>"
        "</PropertyGroup></Project>",
        encoding="utf-8",
    )
    (output / "generated.runtimeconfig.json").write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "tfm": "net11.0",
                    "configProperties": {
                        "System.GC.Server": False,
                        "System.GC.Concurrent": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    package_root = packages / PACKAGE_ID.lower() / VERSION.lower()
    package_runtime = package_root / "runtimes" / "linux-x64" / "native"
    package_runtime.mkdir(parents=True)
    (package_root / f"{PACKAGE_ID.lower()}.{VERSION.lower()}.nupkg.sha512").write_text(
        base64.b64encode(bytes.fromhex(PACKAGE_SHA512)).decode("ascii"),
        encoding="ascii",
    )
    for file_name in ("System.Private.CoreLib.dll", "libcoreclr.so"):
        package_file = package_runtime / file_name
        package_file.write_bytes(file_name.encode("ascii"))
        shutil.copy2(package_file, output / file_name)

    return artifacts, packages


def test_validates_runtime_files_gc_mode_and_exact_pilot_results(tmp_path, monkeypatch):
    artifacts, packages = create_artifact_fixture(tmp_path)
    monkeypatch.setenv("PERFLAB_TARGET_FRAMEWORKS", "net11.0")

    evidence_path = validate_benchmark_artifacts(artifacts, packages, get_profile())

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["tests"] == sorted(EXPECTED_PILOT_TESTS)
    assert {item["name"] for item in evidence["runtimeFiles"]} == {
        "System.Private.CoreLib.dll",
        "libcoreclr.so",
    }


def test_rejects_incomplete_pilot_results(tmp_path, monkeypatch):
    artifacts, packages = create_artifact_fixture(tmp_path)
    report = artifacts / "Byte-perf-lab-report.json"
    contents = json.loads(report.read_text(encoding="utf-8"))
    contents["tests"].pop()
    report.write_text(json.dumps(contents), encoding="utf-8")
    monkeypatch.setenv("PERFLAB_TARGET_FRAMEWORKS", "net11.0")

    with pytest.raises(ValueError, match="exactly the four"):
        validate_benchmark_artifacts(artifacts, packages, get_profile())


def test_rejects_runtime_binary_not_from_selected_package(tmp_path, monkeypatch):
    artifacts, packages = create_artifact_fixture(tmp_path)
    output_corelib = next(tmp_path.glob("generated/**/System.Private.CoreLib.dll"))
    output_corelib.write_bytes(b"different runtime")
    monkeypatch.setenv("PERFLAB_TARGET_FRAMEWORKS", "net11.0")

    with pytest.raises(ValueError, match="does not match"):
        validate_benchmark_artifacts(artifacts, packages, get_profile())


def test_rejects_restored_package_hash_mismatch(tmp_path, monkeypatch):
    artifacts, packages = create_artifact_fixture(tmp_path)
    hash_path = next(packages.glob("**/*.nupkg.sha512"))
    hash_path.write_text(base64.b64encode(b"x" * 64).decode("ascii"), encoding="ascii")
    monkeypatch.setenv("PERFLAB_TARGET_FRAMEWORKS", "net11.0")

    with pytest.raises(ValueError, match="preflight package hash"):
        validate_benchmark_artifacts(artifacts, packages, get_profile())


def test_validates_downloaded_report_and_evidence(tmp_path, monkeypatch):
    artifacts, packages = create_artifact_fixture(tmp_path)
    monkeypatch.setenv("PERFLAB_TARGET_FRAMEWORKS", "net11.0")
    evidence_path = validate_benchmark_artifacts(artifacts, packages, get_profile())
    reports = [
        json.loads((artifacts / "Byte-perf-lab-report.json").read_text(encoding="utf-8")),
        json.loads((artifacts / "Char-perf-lab-report.json").read_text(encoding="utf-8")),
    ]
    results = tmp_path / "results"
    results.mkdir()
    (results / "combined-perf-lab-report.json").write_text(json.dumps(reports), encoding="utf-8")
    shutil.copy2(evidence_path, results / EVIDENCE_FILE_NAME)

    validate_downloaded_results(results, get_profile())
