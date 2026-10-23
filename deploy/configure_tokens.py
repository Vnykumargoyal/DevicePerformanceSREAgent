"""Provision Docker-host credentials without echoing them or using shell arguments."""
import getpass
import os
from pathlib import Path
import secrets
import sys
import tempfile


def validate(token, minimum=1):
    if len(token) < minimum or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("Use a valid raw token without whitespace or a Bearer prefix")
    return token


def save(path, token):
    if path.is_symlink():
        raise ValueError("Refusing a credential-file symlink")
    fd, temporary = tempfile.mkstemp(prefix='.token-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            os.fchown(output.fileno(), 10001, 10001)
            output.write(token + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if not sys.platform.startswith('linux') or os.geteuid() != 0 or not sys.stdin.isatty():
        raise ValueError("Run sudo python3 deploy/configure_tokens.py in an interactive terminal on the Linux Docker host")
    os.umask(0o077)
    root = Path('secrets')
    for path in (root, root / 'mst', root / 'mcp'):
        if path.is_symlink():
            raise ValueError("Refusing a secret-directory symlink")
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
        if path != root:
            path.chown(10001, 10001)
    mcp = root / 'mcp/token'
    if mcp.is_symlink():
        raise ValueError("Refusing a credential-file symlink")
    if mcp.exists():
        validate(mcp.read_text().strip(), minimum=32)
    token = validate(getpass.getpass('Approved MST access token (hidden, without Bearer prefix): ').strip())
    save(root / 'mst/token', token)
    if not mcp.exists():
        save(mcp, secrets.token_urlsafe(48))
    else:
        mcp.chmod(0o600)
        mcp.chown(10001, 10001)
    print('MST token saved; dedicated MCP token ready. Values were not displayed.')
    print('Files: secrets/mst/token and secrets/mcp/token (UID 10001, mode 0600).')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt):
        print('Token setup stopped. Check Linux host, sudo, interactive input and secret paths. No credentials were displayed.', file=sys.stderr)
        raise SystemExit(1)
