#!/usr/bin/env python3
"""Run a tool-free Claude review of a bounded, explicitly prepared prompt packet."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


MAX_PROMPT_BYTES = 512 * 1024


def model_name(value: str) -> str:
    """Accept model aliases and provider IDs without flags or control characters."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}", value):
        raise argparse.ArgumentTypeError("Use a model alias or provider ID without spaces, control characters, or a leading dash (maximum 256 characters).")
    return value


def positive_timeout(value: str) -> float:
    """Require a finite positive subprocess deadline in seconds."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be a positive finite number.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive finite number.")
    return seconds


def read_packet(path: Path) -> bytes:
    """Read only the provided regular UTF-8 file, rejecting unbounded input."""
    if not path.is_file():
        raise ValueError("Prompt must be a regular file.")
    with path.open("rb") as packet_file:
        packet = packet_file.read(MAX_PROMPT_BYTES + 1)
    if len(packet) > MAX_PROMPT_BYTES:
        raise ValueError("Prompt exceeds the 512 KiB limit.")
    try:
        decoded = packet.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Prompt must be UTF-8 text.") from exc
    if not decoded.strip():
        raise ValueError("Prompt must not be empty.")
    return packet


def command(model: str) -> list[str]:
    """Build an invocation that disables tools and local customization loading."""
    return [
        "claude",
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--model",
        model,
        "--output-format",
        "json",
        "--print",
    ]


def assess_response(raw: bytes, returncode: int) -> tuple[dict[str, Any], str | None]:
    """Check the CLI transport and structured result before accepting a review."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}, "Claude exited unsuccessfully." if returncode != 0 else "Claude returned malformed JSON."
    if not isinstance(payload, dict):
        return {}, "Claude exited unsuccessfully." if returncode != 0 else "Claude returned an unexpected JSON shape."
    if returncode != 0:
        return payload, "Claude exited unsuccessfully."
    if payload.get("type") != "result" or payload.get("subtype") != "success" or payload.get("is_error") is not False:
        return payload, "Claude did not report a successful result."
    if not isinstance(payload.get("result"), str) or not payload["result"].strip():
        return payload, "Claude returned an empty review."
    return payload, None


def run_review(packet: bytes, model: str, timeout: float, output_parent: Path | None) -> tuple[Path, bool]:
    """Execute one bounded review and retain private diagnostics without overwriting files."""
    output = Path(tempfile.mkdtemp(prefix="claude-adversarial-review-", dir=output_parent))
    argv = command(model)
    payload: dict[str, Any] = {}
    returncode: int | None = None
    try:
        completed = subprocess.run(argv, input=packet, capture_output=True, timeout=timeout, check=False)
        raw, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        payload, error = assess_response(raw, returncode)
    except subprocess.TimeoutExpired as exc:
        raw, stderr = exc.output or b"", exc.stderr or b""
        error = "Claude exceeded the review timeout."
    except OSError:
        raw, stderr = b"", b""
        error = "Claude could not be started; verify the installed CLI is on PATH."
    (output / "response.json").write_bytes(raw)
    (output / "stderr.log").write_bytes(stderr)
    review = f"# Review incomplete\n\n{error}\n" if error else payload["result"].strip() + "\n"
    (output / "review.md").write_text(review, encoding="utf-8")
    model_usage = payload.get("modelUsage", {})
    metadata = {
        "status": "failed" if error else "success",
        "error": error,
        "requested_model": model,
        "actual_models": sorted(model_usage) if isinstance(model_usage, dict) else [],
        "prompt_bytes": len(packet),
        "timeout_seconds": timeout,
        "returncode": returncode,
        "command": argv,
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return output, error is None


def main(argv: list[str] | None = None) -> int:
    """Validate the review packet and run Claude or print a content-free dry run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True, help="Inspected UTF-8 review packet (maximum 512 KiB).")
    parser.add_argument("--model", type=model_name, default="opus")
    parser.add_argument("--timeout", type=positive_timeout, default=300.0, help="Deadline in seconds (default: 300).")
    parser.add_argument("--output-dir", type=Path, help="Existing parent directory for a unique run directory; defaults to system temp.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and show flags and byte count without running Claude or writing files.")
    args = parser.parse_args(argv)
    try:
        packet = read_packet(args.prompt_file)
        if args.output_dir is not None and not args.output_dir.is_dir():
            raise ValueError("Output parent must be an existing directory.")
    except (OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not read the prompt file or output parent."
        parser.error(message)
    if args.dry_run:
        print(json.dumps({"command": command(args.model), "prompt_bytes": len(packet)}))
        return 0
    try:
        output, success = run_review(packet, args.model, args.timeout, args.output_dir)
    except OSError:
        print("Could not create or write private review artifacts.", file=sys.stderr)
        return 1
    print(f"Review {'completed' if success else 'failed'}. Artifacts: {output}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
