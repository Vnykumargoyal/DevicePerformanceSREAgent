import argparse
import asyncio
import json
import logging
import os
import getpass
import sys
from pathlib import Path
from dataclasses import replace

from dotenv import load_dotenv

from .monitor import run_worker
from .settings import Settings, normalize_serial_numbers
from .store import Store
from .tools import Tools


def main():
    os.umask(0o077)
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Device performance MST connector and 15-minute monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("poll", "Poll every configured interval (default 900 seconds)"),
                               ("once", "One authorized MST polling run"),
                               ("summary", "Print cached fleet summary without accessing MST")):
        command_parser = commands.add_parser(command, help=help_text)
        command_parser.add_argument("--serial-numbers", nargs="+", help="One or more serial strings supplied at runtime")
    commands.add_parser("demo", help="Two synthetic polls in an isolated synthetic-demo subdirectory")
    commands.add_parser("configure-token", help="Save an approved MST token using hidden terminal input")
    commands.add_parser("doctor", help="Check local configuration and credential availability without API calls")
    server = commands.add_parser("serve", help="Read-only MCP server; worker is a separate process")
    server.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.command == "configure-token":
            from .credentials import save_mst_token
            if not sys.stdin.isatty():
                raise ValueError("Run configure-token in your interactive terminal; token input must be hidden")
            try:
                token = getpass.getpass("Paste approved MST access token (hidden, without Bearer prefix): ")
            except EOFError:
                raise ValueError("Token entry was cancelled; no credential was saved") from None
            result = save_mst_token(token)
            print(json.dumps(result, indent=2))
            return
        settings = Settings.load()
        if args.command == "doctor":
            from .credentials import local_readiness
            result = local_readiness(settings)
            print(json.dumps(result, indent=2))
            if result["mst_credential"]["status"] != "available":
                raise SystemExit(2)
            return
        serial_numbers = getattr(args, "serial_numbers", None)
        if serial_numbers is not None:
            settings = replace(settings, devices=normalize_serial_numbers(serial_numbers))
        if args.command == "serve":
            from .server import serve
            serve(settings, args.transport)
            return
        if args.command == "demo":
            from .demo import run_demo
            result = asyncio.run(run_demo(settings))
        elif args.command in ("poll", "once"):
            if args.command == "poll":
                logging.getLogger("device_sre.monitor").setLevel(logging.INFO)
            result = asyncio.run(run_worker(settings, once=args.command == "once"))
        else:
            result = Tools(settings, Store(settings.data_dir)).get_fleet_summary(serial_numbers=serial_numbers)
        if result is not None:
            print(json.dumps(result, indent=2, allow_nan=False))
        if args.command == "once" and result["failed"]:
            raise SystemExit(2)
    except ValueError as exc:
        # Validation and secret-reader messages are controlled locally; no upstream bodies.
        parser.exit(1, f"Configuration error: {exc}\n")
    except OSError:
        parser.exit(1, "Storage error: check the configured data directory and file permissions.\n")
    except KeyboardInterrupt:
        pass

