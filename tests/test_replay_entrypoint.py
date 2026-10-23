import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "replay_mcp.py"


@pytest.mark.parametrize("file_arguments", [[], ["--file", "/does-not-exist/old-device.json"]])
def test_default_command_dispatches_to_live_mcp_and_never_opens_the_supplied_file(monkeypatch, file_arguments, capsys):
    namespace = runpy.run_path(str(SCRIPT))
    captured = []
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *file_arguments, "--serial-numbers", "000001", "000002", "000001"])
    monkeypatch.setattr(os, "execv", lambda command, arguments: captured.append((command, arguments)))
    namespace["main"]()
    command, arguments = captured[0]
    assert command == sys.executable
    assert arguments == [sys.executable, str(ROOT / "scripts" / "check_mcp.py"),
                         "--transport", "stdio", "--serial-numbers", "000001", "000002", "--details-only"]
    if file_arguments:
        assert "--file is ignored" in capsys.readouterr().err


def test_legacy_file_command_runs_actual_stdio_mcp_instead_of_rejecting_a_different_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing-token"))
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--file", str(ROOT / "tests" / "fixtures" / "device_response.json"),
        "--serial-numbers", "ANOTHER-DEVICE",
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2, completed.stderr
    result = json.loads(completed.stdout)
    assert result["requested"] == 1
    assert result["devices"][0]["device_id"] == "ANOTHER-DEVICE"
    assert result["devices"][0]["error_code"] == "mst_credential_unavailable"
    assert result["devices"][0]["request_source"] == "mst"
    assert "--file is ignored" in completed.stderr
    assert "Offline replay stopped" not in completed.stderr


@pytest.mark.parametrize("arguments, expected", [
    (["--offline"], "--offline requires at least one --file"),
    (["--assessed-at", "2026-10-22T06:00:00Z"], "--assessed-at requires --offline"),
])
def test_mode_options_are_validated_before_any_mcp_call(arguments, expected):
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--serial-numbers", "ANY-DEVICE", *arguments,
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert expected in completed.stderr
    assert "Processing request" not in completed.stderr


@pytest.mark.parametrize("entered, expected", [
    ("000001", ["000001"]),
    ("000001,000002 000001", ["000001", "000002"]),
])
def test_omitted_argument_prompts_and_uses_only_the_users_serials(monkeypatch, capsys, entered, expected):
    namespace = runpy.run_path(str(SCRIPT))
    captured = []
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: entered)
    monkeypatch.setattr(os, "execv", lambda command, arguments: captured.append(arguments))
    namespace["main"]()
    arguments = captured[0]
    assert arguments[arguments.index("--serial-numbers") + 1:-1] == expected
    assert arguments[-1] == "--details-only"
    output = capsys.readouterr()
    assert "Enter device serial number(s)" in output.err
    assert not output.out  # stdout stays available for the device-details JSON.


@pytest.mark.parametrize("entered", ["", " ,  ", "invalid/serial"])
def test_empty_or_invalid_user_input_stops_without_a_default_device(monkeypatch, entered):
    namespace = runpy.run_path(str(SCRIPT))
    captured = []
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: entered)
    monkeypatch.setattr(os, "execv", lambda *arguments: captured.append(arguments))
    with pytest.raises(SystemExit) as exc:
        namespace["main"]()
    assert exc.value.code == 2
    assert not captured


def test_missing_argument_without_terminal_fails_instead_of_waiting_for_input():
    completed = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert "Pass --serial-numbers" in completed.stderr
    assert not completed.stdout
