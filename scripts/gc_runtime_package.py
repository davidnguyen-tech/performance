#!/usr/bin/env python3

"""Validate and verify an opt-in GC runtime package performance job."""

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import xml.etree.ElementTree as ET

FEED_INDEX = "https://pkgs.dev.azure.com/dnceng/public/_packaging/dotnet-experimental/nuget/v3/index.json"
PACKAGE_ID = "Microsoft.NETCore.App.Runtime.linux-x64"
RUNTIME_REPOSITORY = "https://github.com/dotnet/runtimelab"
TARGET_FRAMEWORK = "net11.0"
EVIDENCE_FILE_NAME = "gc-runtime-package-evidence.json"
RUNTIME_FILES = ("System.Private.CoreLib.dll", "libcoreclr.so")
EXPECTED_PILOT_TESTS = frozenset(
    {
        f"System.Tests.Perf_GC<{element_type}>.NewOperator_Array(length: {length})"
        for element_type in ("Byte", "Char")
        for length in (1000, 10_000)
    }
)


def validate_profile(version, commit, branch, queue, build_id):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9-]{1,7}(?:\.[A-Za-z0-9-]+)*", version):
        raise ValueError("GC runtime package version must be an exact experiment version with a short prerelease label.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("GC runtime source commit must be a complete Git SHA.")
    if not re.fullmatch(r"feature/gc/[A-Za-z0-9][A-Za-z0-9/_-]*", branch) or "//" in branch or branch.endswith("/"):
        raise ValueError("GC runtime source branch must identify a feature/gc experiment.")
    if branch == "feature/gc/baseline" and not re.match(r"[0-9]+\.[0-9]+\.[0-9]+-gcbase(?:\.|$)", version):
        raise ValueError("The baseline branch must use the gcbase prerelease label.")
    if not re.fullmatch(r"Ubuntu\.2204\.Amd64(?:\.[A-Za-z0-9_-]+)*\.Perf", queue):
        raise ValueError("Supply an explicitly approved Ubuntu 22.04 x64 performance queue; there is no default queue.")
    if not re.fullmatch(r"[1-9][0-9]*", build_id):
        raise ValueError("GC runtime producer build ID must be a positive Azure DevOps build number.")
    return {
        "GcRuntimePackageVersion": version,
        "GcRuntimeCommit": commit.lower(),
        "GcRuntimeBranch": branch,
        "GcPerformanceQueue": queue,
        "GcRuntimeBuildId": build_id,
        "GcRuntimePackageId": PACKAGE_ID,
        "GcRuntimeFeed": FEED_INDEX,
        "GcTargetFramework": TARGET_FRAMEWORK,
    }


def validate_package_metadata(xml, version, commit):
    root = ET.fromstring(xml)
    namespace = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
    if root.tag != namespace + "package":
        raise ValueError("The runtime package returned an invalid NuGet manifest.")
    metadata = root.find(namespace + "metadata")
    if metadata is None:
        raise ValueError("The runtime package has no NuGet metadata.")
    package_id = metadata.findtext(namespace + "id")
    package_version = metadata.findtext(namespace + "version")
    repository = metadata.find(namespace + "repository")
    if package_id is None or package_id.lower() != PACKAGE_ID.lower() or package_version is None or package_version.lower() != version.lower():
        raise ValueError("The restored package identity does not match the requested runtime package.")
    if repository is None:
        raise ValueError("The runtime package does not record its source repository and commit.")
    repository_url = repository.get("url", "").rstrip("/").removesuffix(".git")
    if repository_url != RUNTIME_REPOSITORY or repository.get("commit", "").lower() != commit.lower():
        raise ValueError("The runtime package source does not match the requested runtimelab commit.")


def _get_package_base_address():
    with urlopen(FEED_INDEX, timeout=30) as response:
        index = json.load(response)
    resources = index.get("resources") if isinstance(index, dict) else None
    if not isinstance(resources, list):
        raise ValueError("The experimental feed returned an invalid service index.")
    addresses = [
        resource["@id"] for resource in resources
        if isinstance(resource, dict)
        and str(resource.get("@type", "")).startswith("PackageBaseAddress/3.0.0")
        and resource.get("@id", "").startswith("https://pkgs.dev.azure.com/dnceng/")
    ]
    if len(addresses) != 1:
        raise ValueError("The experimental feed returned an unexpected package-content endpoint.")
    return addresses[0].rstrip("/")


def fetch_package_evidence(version):
    base_address = _get_package_base_address()
    package = PACKAGE_ID.lower()
    package_url = f"{base_address}/{package}/{version.lower()}/{package}"
    with urlopen(f"{package_url}.nuspec", timeout=30) as response:
        metadata = response.read()
    package_sha512 = hashlib.sha512()
    with urlopen(f"{package_url}.{version.lower()}.nupkg", timeout=30) as response:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            package_sha512.update(block)
    return metadata, package_sha512.hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_package_sha512(value):
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("The package cache contains an invalid SHA-512 digest.") from error
    if len(decoded) != hashlib.sha512().digest_size:
        raise ValueError("The package cache contains an invalid SHA-512 digest.")
    return decoded.hex()


def _require_profile_value(profile, name):
    value = profile.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing runtime package evidence value: {name}.")
    return value


def _validate_report_documents(reports, profile):
    expected_additional_data = {
        "RUNTIME_PACKAGE_BUILD_ID": _require_profile_value(profile, "GcRuntimeBuildId"),
        "RUNTIME_PACKAGE_COMMIT": _require_profile_value(profile, "GcRuntimeCommit"),
        "RUNTIME_PACKAGE_FEED": FEED_INDEX,
        "RUNTIME_PACKAGE_ID": PACKAGE_ID,
        "RUNTIME_PACKAGE_RID": "linux-x64",
        "RUNTIME_PACKAGE_SHA512": _require_profile_value(profile, "GcRuntimePackageSha512"),
        "PERFLAB_RUNTIME_PACKAGE_VERSION": _require_profile_value(profile, "GcRuntimePackageVersion"),
        "targetFrameworks": TARGET_FRAMEWORK,
    }
    expected_branch = _require_profile_value(profile, "GcRuntimeBranch")
    expected_queue = _require_profile_value(profile, "GcPerformanceQueue")
    test_names = []

    if not reports:
        raise ValueError("The GC runtime package run produced no PerfLab reports.")

    for report in reports:
        if not isinstance(report, dict) or report.get("inLab") is not True:
            raise ValueError("The GC runtime package run produced a malformed or non-lab report.")
        build = report.get("build")
        run = report.get("run")
        if not isinstance(build, dict) or not isinstance(run, dict):
            raise ValueError("The GC runtime package report is missing build or run metadata.")
        if (
            build.get("repo") != "dotnet/runtimelab"
            or build.get("branch") != expected_branch
            or str(build.get("gitHash", "")).lower() != profile["GcRuntimeCommit"]
            or build.get("architecture") != "x64"
            or run.get("queue") != expected_queue
            or not str(run.get("name", "")).startswith("gc-runtime-package-")
            or not re.fullmatch(r"[0-9a-fA-F]{40}", str(run.get("perfRepoHash", "")))
        ):
            raise ValueError("The GC runtime package report does not match the requested candidate identity.")
        additional_data = build.get("additionalData")
        if not isinstance(additional_data, dict):
            raise ValueError("The GC runtime package report has no additional package metadata.")
        for name, value in expected_additional_data.items():
            if additional_data.get(name) != value:
                raise ValueError(f"The GC runtime package report has unexpected {name} metadata.")
        if not additional_data.get("BenchmarkDotNetVersion") or not additional_data.get("sdkVersion"):
            raise ValueError("The GC runtime package report is missing harness or SDK identity.")

        tests = report.get("tests")
        if not isinstance(tests, list):
            raise ValueError("The GC runtime package report has no benchmark test list.")
        for test in tests:
            if not isinstance(test, dict) or test.get("categories") != ["Runtime"]:
                raise ValueError("The GC runtime package report contains an unexpected benchmark category.")
            test_additional_data = test.get("additionalData")
            if not isinstance(test_additional_data, dict):
                raise ValueError("The GC runtime package report contains invalid test metadata.")
            if test_additional_data.get("criticalErrors") == "true":
                raise ValueError("The GC runtime package report contains critical benchmark errors.")
            test_names.append(test.get("name"))
            counters = test.get("counters")
            if not isinstance(counters, list):
                raise ValueError("The GC runtime package report contains an invalid counter list.")
            duration = next(
                (
                    counter
                    for counter in counters
                    if isinstance(counter, dict)
                    if counter.get("name") == "Duration of single invocation"
                ),
                None,
            )
            results = duration.get("results") if isinstance(duration, dict) else None
            if (
                not isinstance(results, list)
                or not results
                or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in results)
            ):
                raise ValueError("The GC runtime package report contains an empty or invalid measurement.")

    if len(test_names) != len(set(test_names)):
        raise ValueError("The GC runtime package run produced duplicate benchmark cases.")
    if set(test_names) != EXPECTED_PILOT_TESTS:
        raise ValueError(
            "The GC runtime package run must produce exactly the four Perf_GC allocation cases."
        )
    return sorted(test_names)


def _find_generated_project(output_directory):
    for directory in (output_directory, *output_directory.parents):
        project = directory / "BenchmarkDotNet.Autogenerated.csproj"
        if project.is_file():
            return project
    raise ValueError("Could not locate the generated BenchmarkDotNet project.")


def _find_package_runtime_file(package_root, rid, file_name):
    rid_segment = f"/runtimes/{rid}/"
    candidates = [
        path
        for path in package_root.rglob(file_name)
        if rid_segment in "/" + path.relative_to(package_root).as_posix()
    ]
    if len(candidates) != 1:
        raise ValueError(f"Expected one {file_name} in the selected runtime package.")
    return candidates[0]


def validate_benchmark_artifacts(artifacts_directory, packages_directory, profile):
    artifacts_directory = Path(artifacts_directory)
    packages_directory = Path(packages_directory)
    report_paths = sorted(artifacts_directory.glob("**/*-perf-lab-report.json"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_paths]
    test_names = _validate_report_documents(reports, profile)

    log_paths = sorted(artifacts_directory.glob("BenchmarkRun-*.log"))
    if len(log_paths) != 1:
        raise ValueError("Expected one BenchmarkDotNet run log for the GC package pilot.")
    log_text = log_paths[0].read_text(encoding="utf-8")
    for test_name in EXPECTED_PILOT_TESTS:
        if f'--benchmarkName "{test_name}"' not in log_text:
            raise ValueError(f"The BenchmarkDotNet log is missing execution evidence for {test_name}.")

    runtime_lines = re.findall(r"^// Runtime=(.+)$", log_text, re.MULTILINE)
    gc_lines = re.findall(r"^// GC=(.+)$", log_text, re.MULTILINE)
    if len(runtime_lines) != len(EXPECTED_PILOT_TESTS) or len(set(runtime_lines)) != 1:
        raise ValueError("The GC package pilot did not use one consistent runtime for all four cases.")
    if gc_lines != ["Concurrent Workstation"] * len(EXPECTED_PILOT_TESTS):
        raise ValueError("The GC package pilot did not use concurrent Workstation GC for all four cases.")

    version = _require_profile_value(profile, "GcRuntimePackageVersion")
    version_core, prerelease = version.split("-", 1)
    prerelease_label = prerelease.split(".", 1)[0]
    runtime_description = runtime_lines[0]
    if version_core not in runtime_description or prerelease_label not in runtime_description:
        raise ValueError("The measured runtime does not identify the selected experiment package version.")

    output_directories = {
        Path(match.strip().strip('"'))
        for match in re.findall(r"^// Execute: .* in (.+)$", log_text, re.MULTILINE)
    }
    if len(output_directories) != 1:
        raise ValueError("Expected one generated benchmark output directory for the GC package pilot.")
    output_directory = output_directories.pop()
    if not output_directory.is_dir():
        raise ValueError("The generated benchmark output directory no longer exists; use --keepFiles.")

    rid = "linux-x64"
    package_root = (
        packages_directory
        / PACKAGE_ID.lower()
        / version.lower()
    )
    if not package_root.is_dir():
        raise ValueError("The selected runtime package is missing from the benchmark package cache.")
    package_hash_path = package_root / f"{PACKAGE_ID.lower()}.{version.lower()}.nupkg.sha512"
    if not package_hash_path.is_file():
        raise ValueError("The selected runtime package cache entry has no SHA-512 digest.")
    package_sha512 = _decode_package_sha512(package_hash_path.read_text(encoding="ascii"))
    if package_sha512 != profile["GcRuntimePackageSha512"]:
        raise ValueError("The restored runtime package does not match the preflight package hash.")

    runtime_files = []
    for file_name in RUNTIME_FILES:
        output_file = output_directory / file_name
        package_file = _find_package_runtime_file(package_root, rid, file_name)
        if not output_file.is_file():
            raise ValueError(f"The generated benchmark is not self-contained; {file_name} is missing.")
        output_sha256 = _sha256(output_file)
        package_sha256 = _sha256(package_file)
        if output_sha256 != package_sha256:
            raise ValueError(f"The generated benchmark's {file_name} does not match the selected runtime package.")
        runtime_files.append(
            {
                "name": file_name,
                "sha256": output_sha256,
                "packagePath": package_file.relative_to(package_root).as_posix(),
            }
        )

    generated_project = _find_generated_project(output_directory)
    generated_project_text = generated_project.read_text(encoding="utf-8-sig")
    if (
        "<ServerGarbageCollection>false</ServerGarbageCollection>" not in generated_project_text
        or "<ConcurrentGarbageCollection>true</ConcurrentGarbageCollection>" not in generated_project_text
    ):
        raise ValueError("The generated benchmark project does not preserve the requested GC settings.")

    runtime_configs = sorted(output_directory.glob("*.runtimeconfig.json"))
    generated_runtime_configs = [
        path
        for path in runtime_configs
        if path.stem.startswith(generated_project.parent.name)
    ]
    if len(generated_runtime_configs) != 1:
        generated_runtime_configs = runtime_configs
    if len(generated_runtime_configs) != 1:
        raise ValueError("Expected one generated benchmark runtime configuration.")
    runtime_config = json.loads(generated_runtime_configs[0].read_text(encoding="utf-8"))
    runtime_options = runtime_config.get("runtimeOptions", {})
    config_properties = runtime_options.get("configProperties", {})
    if (
        runtime_options.get("tfm") != _require_profile_value(profile, "GcTargetFramework")
        or config_properties.get("System.GC.Server") is not False
        or config_properties.get("System.GC.Concurrent") is not True
        or "framework" in runtime_options
    ):
        raise ValueError("The generated benchmark runtime configuration is not the expected self-contained GC job.")

    evidence = {
        "profile": profile,
        "tests": test_names,
        "runtimeDescription": runtime_description,
        "gcMode": "Concurrent Workstation",
        "packageSha512": package_sha512,
        "generatedProject": generated_project.name,
        "runtimeConfig": generated_runtime_configs[0].name,
        "runtimeFiles": runtime_files,
    }
    evidence_path = artifacts_directory / EVIDENCE_FILE_NAME
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_path


def validate_downloaded_results(results_directory, profile):
    results_directory = Path(results_directory)
    combined_paths = sorted(results_directory.glob("**/*combined-perf-lab-report.json"))
    if len(combined_paths) != 1:
        raise ValueError("Expected one downloaded combined PerfLab report for the GC package pilot.")
    reports = json.loads(combined_paths[0].read_text(encoding="utf-8"))
    test_names = _validate_report_documents(reports, profile)

    evidence_paths = sorted(results_directory.glob(f"**/{EVIDENCE_FILE_NAME}"))
    if len(evidence_paths) != 1:
        raise ValueError("The GC runtime package evidence file was not downloaded from Helix.")
    evidence = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    if evidence.get("profile") != profile or evidence.get("tests") != test_names:
        raise ValueError("The downloaded GC runtime package evidence does not match the requested run.")
    runtime_files = evidence.get("runtimeFiles")
    if (
        not isinstance(runtime_files, list)
        or any(not isinstance(item, dict) for item in runtime_files)
        or {item.get("name") for item in runtime_files} != set(RUNTIME_FILES)
    ):
        raise ValueError("The downloaded GC runtime package evidence is missing runtime binary hashes.")


def profile_from_perflab_environment(environment=os.environ):
    profile = validate_profile(
        environment.get("PERFLAB_RUNTIME_PACKAGE_VERSION", ""),
        environment.get("PERFLAB_HASH", ""),
        environment.get("PERFLAB_BRANCH", ""),
        environment.get("PERFLAB_QUEUE", ""),
        environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_BUILD_ID", ""),
    )
    package_sha512 = environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_SHA512", "")
    if not re.fullmatch(r"[0-9a-f]{128}", package_sha512):
        raise ValueError("GC runtime package SHA-512 must be a lowercase hexadecimal digest.")
    if environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_ID") != PACKAGE_ID:
        raise ValueError("GC runtime package ID metadata is missing or invalid.")
    if environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_RID") != "linux-x64":
        raise ValueError("GC runtime package RID metadata is missing or invalid.")
    if environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_FEED") != FEED_INDEX:
        raise ValueError("GC runtime package feed metadata is missing or invalid.")
    if environment.get("PERFLAB_DATA_RUNTIME_PACKAGE_COMMIT", "").lower() != profile["GcRuntimeCommit"]:
        raise ValueError("GC runtime package commit metadata is missing or invalid.")
    profile["GcRuntimePackageSha512"] = package_sha512
    return profile


def _profile_from_environment(prefix):
    profile = validate_profile(
        os.environ.get(f"{prefix}PACKAGE_VERSION", ""),
        os.environ.get(f"{prefix}COMMIT", ""),
        os.environ.get(f"{prefix}BRANCH", ""),
        os.environ.get(f"{prefix}PERFORMANCE_QUEUE", ""),
        os.environ.get(f"{prefix}BUILD_ID", ""),
    )
    package_sha512 = os.environ.get(f"{prefix}PACKAGE_SHA512")
    if package_sha512:
        if not re.fullmatch(r"[0-9a-f]{128}", package_sha512):
            raise ValueError("GC runtime package SHA-512 must be a lowercase hexadecimal digest.")
        profile["GcRuntimePackageSha512"] = package_sha512
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-results")
    args = parser.parse_args(argv)

    try:
        if args.validate_results:
            profile = _profile_from_environment("GC_RUNTIME_")
            if "GcRuntimePackageSha512" not in profile:
                raise ValueError("GC runtime package SHA-512 is required for result validation.")
            validate_downloaded_results(args.validate_results, profile)
            return 0

        if os.environ.get("BUILD_REASON") != "Manual" or os.environ.get("SYSTEM_TEAMPROJECT") != "internal":
            raise ValueError("GC package performance jobs require an explicitly approved manual internal run.")
        if os.environ.get("GC_ONLY_SANITY_CHECK", "false").lower() == "true":
            raise ValueError("The GC package pilot cannot use the generic, broader sanity-check filter.")
        profile = _profile_from_environment("GC_RUNTIME_")
        metadata, package_sha512 = fetch_package_evidence(profile["GcRuntimePackageVersion"])
        validate_package_metadata(
            metadata,
            profile["GcRuntimePackageVersion"],
            profile["GcRuntimeCommit"])
        profile["GcRuntimePackageSha512"] = package_sha512
    except HTTPError as error:
        print(f"GC runtime package validation failed: HTTP {error.code} from the package feed.", file=sys.stderr)
        error.close()
        return 1
    except (OSError, ValueError, ET.ParseError, URLError, json.JSONDecodeError) as error:
        print(f"GC runtime package validation failed: {error}", file=sys.stderr)
        return 1

    from performance.common import set_environment_variable
    for name, value in profile.items():
        set_environment_variable(name, value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
