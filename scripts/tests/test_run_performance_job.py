import pytest

import scripts.run_performance_job as run_performance_job
from scripts.run_performance_job import APT_LOCK_TIMEOUT_OPTION, get_pre_commands, get_work_item_command


def get_generated_apt_commands(*, internal: bool, runtime_type: str) -> list[str]:
    pre_commands = get_pre_commands(
        os_group="linux",
        os_distro="ubuntu",
        internal=internal,
        runtime_type=runtime_type,
        codegen_type="jit",
        build_config="Release",
        v8_version="12.0.0",
        wasm_local_package_version="11.0.0-ci" if runtime_type == "wasm_coreclr" else None,
    )

    return [
        command.strip()
        for pre_command in pre_commands
        for command in pre_command.split(" && ")
        if command.strip().startswith(("sudo apt ", "sudo apt-get "))
    ]


@pytest.mark.parametrize(
    ("internal", "runtime_type", "expected_command_count"),
    [
        (True, "coreclr", 4),
        (False, "wasm", 6),
        (True, "wasm", 10),
        (False, "wasm_coreclr", 6),
    ],
)
def test_all_generated_apt_commands_use_lock_timeout(
    internal: bool, runtime_type: str, expected_command_count: int
):
    apt_commands = get_generated_apt_commands(
        internal=internal, runtime_type=runtime_type
    )

    assert len(apt_commands) == expected_command_count
    assert all(
        f" {APT_LOCK_TIMEOUT_OPTION} " in command for command in apt_commands
    )


def test_generated_prerequisites_do_not_poll_dpkg_lock():
    pre_commands = get_pre_commands(
        os_group="linux",
        os_distro="ubuntu",
        internal=True,
        runtime_type="wasm",
        codegen_type="jit",
        build_config="Release",
        v8_version="12.0.0",
    )

    prerequisites = "\n".join(pre_commands)
    assert "fuser" not in prerequisites
    assert "Waiting for dpkg" not in prerequisites


@pytest.mark.parametrize(
    ("internal", "skip_upload", "expected_upload"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_upload_opt_out_does_not_change_runtime_selection(internal, skip_upload, expected_upload):
    command = get_work_item_command(
        os_group="linux",
        target_csproj="src/benchmarks/micro/MicroBenchmarks.csproj",
        architecture="x64",
        perf_lab_framework="net11.0",
        internal=internal,
        wasm=False,
        bdn_artifacts_dir="$HELIX_WORKITEM_UPLOAD_ROOT/BenchmarkDotNet.Artifacts",
        skip_perflab_upload=skip_upload,
    )
    assert ("--upload-to-perflab-container" in command) is expected_upload
    assert "--dotnet-versions" in command
    assert "net11.0" in command


@pytest.mark.parametrize(
    ("extra_arguments", "expected_wait"),
    [([], True), (["--no-wait-for-work-item-completion"], False)],
)
def test_parses_work_item_completion_mode(monkeypatch, extra_arguments, expected_wait):
    captured = {}
    monkeypatch.setattr(
        run_performance_job,
        "run_performance_job",
        lambda args: captured.update(wait=args.wait_for_work_item_completion),
    )

    run_performance_job.main(
        [
            "run_performance_job.py",
            "--run-kind",
            "micro",
            "--architecture",
            "x64",
            "--os-group",
            "linux",
            *extra_arguments,
        ]
    )

    assert captured["wait"] is expected_wait
