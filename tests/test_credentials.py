import json
from pathlib import Path
import stat
import sys

import pytest

from device_sre import cli, credentials
from device_sre.credentials import local_readiness, save_mst_token
from device_sre.settings import Settings, read_secret


def test_token_is_saved_privately_and_rotated_atomically(tmp_path, monkeypatch):
    path = tmp_path / "private" / "mst-token"
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))
    result = save_mst_token("fixture-first-token")
    assert path.read_text() == "fixture-first-token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "fixture-first-token" not in json.dumps(result)
    save_mst_token("fixture-second-token")
    assert read_secret("MST_TOKEN_FILE", "MST_BEARER_TOKEN") == "fixture-second-token"
    assert not list(path.parent.glob(".mst-token-*"))


@pytest.mark.parametrize("value", ["", "   ", "Bearer fixture-token", "fixture\nsecond", "non-ascii-\u00e9"])
def test_invalid_token_never_replaces_existing_credential(tmp_path, monkeypatch, value):
    path = tmp_path / "mst-token"
    path.write_text("existing-token")
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))
    with pytest.raises(ValueError):
        save_mst_token(value)
    assert path.read_text() == "existing-token"


def test_unconfigured_path_and_symlink_are_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="Set MST_TOKEN_FILE"):
        save_mst_token("fixture-token")
    actual = tmp_path / "target"
    actual.write_text("existing")
    link = tmp_path / "link"
    link.symlink_to(actual)
    monkeypatch.setenv("MST_TOKEN_FILE", str(link))
    with pytest.raises(ValueError, match="regular file"):
        save_mst_token("fixture-token")
    assert actual.read_text() == "existing"


def test_failed_atomic_replace_preserves_existing_token_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "mst-token"
    path.write_text("existing")
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(credentials.os, "replace", fail)
    with pytest.raises(OSError):
        save_mst_token("fixture-token")
    assert path.read_text() == "existing"
    assert not list(tmp_path.glob(".mst-token-*"))


def test_doctor_reports_credential_availability_without_values_or_network(tmp_path, monkeypatch):
    path = tmp_path / "mst-token"
    path.write_text("fixture-mst-secret")
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))
    monkeypatch.delenv("SRE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("SRE_MCP_BEARER_TOKEN", "private-fixture-mcp-value-" + "x" * 40)
    result = local_readiness(Settings(data_dir=tmp_path))
    encoded = json.dumps(result)
    assert result["status"] == "ready_for_live_test"
    assert result["mst_credential"]["status"] == "available"
    assert result["http_mcp_credential"]["status"] == "available"
    assert result["network_called"] is False and result["live_authentication_verified"] is False
    assert "fixture-mst-secret" not in encoded and "private-fixture-mcp-value" not in encoded


def test_missing_file_has_priority_over_environment_token_in_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-mst-secret")
    monkeypatch.delenv("SRE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("SRE_MCP_BEARER_TOKEN", "too-short")
    result = local_readiness(Settings(data_dir=tmp_path))
    assert result["status"] == "mst_credential_required"
    assert result["mst_credential"]["status"] == "unavailable"
    assert result["mst_credential"]["source"] == "file"
    assert result["http_mcp_credential"]["status"] == "unavailable"
    assert "fixture-mst-secret" not in json.dumps(result)


def test_configure_token_cli_uses_hidden_prompt_and_never_echoes_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "mst-token"
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))
    monkeypatch.setattr(sys, "argv", ["device-sre", "configure-token"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "fixture-hidden-value")
    cli.main()
    captured = capsys.readouterr()
    assert "fixture-hidden-value" not in captured.out + captured.err
    assert json.loads(captured.out)["status"] == "saved"
    assert path.read_text().strip() == "fixture-hidden-value"


def test_configure_token_cli_refuses_noninteractive_input(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "mst-token"
    monkeypatch.setenv("MST_TOKEN_FILE", str(path))
    monkeypatch.setattr(sys, "argv", ["device-sre", "configure-token"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert not path.exists()


def test_doctor_cli_exits_two_for_missing_mst_credential(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "no-token"))
    monkeypatch.setattr(sys, "argv", ["device-sre", "doctor"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "mst_credential_required" and not result["network_called"]


def test_home_relative_token_path_is_consistent_between_setup_and_reader(tmp_path, monkeypatch):
    # expanduser uses the account home on this machine; monkeypatch the expansion,
    # rather than replacing the HOME environment variable.
    original = Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda path: tmp_path / "token" if str(path) == "~/token" else original(path))
    monkeypatch.setenv("MST_TOKEN_FILE", "~/token")
    save_mst_token("fixture-value")
    assert read_secret("MST_TOKEN_FILE", "MST_BEARER_TOKEN") == "fixture-value"
