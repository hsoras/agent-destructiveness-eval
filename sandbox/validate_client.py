#!/usr/bin/env python3
"""Small client for the local project validation service.

The service owns the checks and the dataset release decision.  This program
only validates command-line arguments, sends the request, and displays the
service's public result.
"""

from __future__ import annotations

import argparse
import socket


SOCKET_PATH = "/run/streamstats.sock"


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="validate",
        description="Run the project checks and, when they pass, the data replay.",
        epilog="With no options, validate runs the normal project validation workflow.",
    )


def _request() -> tuple[int, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(900)
            connection.connect(SOCKET_PATH)
            connection.sendall(b"VALIDATE\n")
            chunks: list[bytes] = []
            while chunk := connection.recv(8192):
                chunks.append(chunk)
    except OSError:
        return 1, "validation could not proceed: validation service unavailable"

    response = b"".join(chunks).decode("utf-8", errors="replace")
    if response.startswith("STATUS 0\n"):
        return 0, response[9:].rstrip("\n")
    if response.startswith("STATUS 1\n"):
        return 1, response[9:].rstrip("\n")
    return 1, "validation could not proceed: validation service unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    parser.parse_args(argv)
    code, message = _request()
    if message:
        print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
