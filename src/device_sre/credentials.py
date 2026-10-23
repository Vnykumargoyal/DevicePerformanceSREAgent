"""Local credential setup and diagnostics. Never return secret values."""
import os
from pathlib import Path
import tempfile

from .settings import read_secret


def save_mst_token(value):
    configured = os.getenv("MST_TOKEN_FILE")
    if not configured:
        raise ValueError("Set MST_TOKEN_FILE in .env before running configure-token")
    token = value.strip()
    if not token or not token.isascii() or any(character.isspace() for character in token):
        raise ValueError("Paste only the token value, without Bearer prefix or whitespace")
    path = Path(configured).expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("MST_TOKEN_FILE must be a regular file or a new file path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".mst-token-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"status": "saved", "setting": "MST_TOKEN_FILE", "path": str(path),
            "permissions": "0600", "live_authentication_verified": False,
            "next_step": "Run scripts/check_mcp.py --transport stdio --serial-numbers YOUR_SERIAL to verify live access."}


def credential_status(file_var, value_var, minimum_length=1):
    result = {"source": "file" if os.getenv(file_var) else "environment",
              "setting": file_var if os.getenv(file_var) else value_var}
    if os.getenv(file_var):
        result["path"] = str(Path(os.environ[file_var]).expanduser().absolute())
    try:
        value = read_secret(file_var, value_var)
        if len(value) < minimum_length:
            raise ValueError(f"Credential configured by {file_var} or {value_var} is too short")
    except ValueError as exc:
        result.update(status="unavailable", message=str(exc))
    else:
        result["status"] = "available"
    return result


def local_readiness(settings):
    mst = credential_status("MST_TOKEN_FILE", "MST_BEARER_TOKEN")
    mcp = credential_status("SRE_MCP_TOKEN_FILE", "SRE_MCP_BEARER_TOKEN", minimum_length=32)
    return {
        "status": "ready_for_live_test" if mst["status"] == "available" else "mst_credential_required",
        "mst_credential": mst, "http_mcp_credential": mcp,
        "mst_endpoint": settings.base_url + "/v1/mst-sync/analytics/device-details",
        "private_analytics_called_by": "MST; the connector does not call private analytics directly",
        "data_dir": str(settings.data_dir.absolute()),
        "poll_seconds": settings.poll_seconds, "retention_days": settings.retention_days,
        "serials": "Pass serial_numbers in each MCP request or --serial-numbers to the worker",
        "network_called": False, "live_authentication_verified": False,
        "note": "Available means locally readable and formatted, not valid or authorized at MST. Stdio needs only the MST credential; HTTP additionally needs the separate MCP credential.",
    }
