#!/usr/bin/env python3
"""Run an independent Claude review using a private source snapshot or prompt packet."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, Protocol

MAX_PROMPT_BYTES = 512 * 1024
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOCUS = (
    "security, performance, code correctness, frontend and accessibility, architecture, reliability, privacy, compatibility, testing, operations"
)


class ReviewContext(Protocol):
    """A bounded, inspected collection of source and change evidence."""

    prompt: str
    metadata: dict[str, Any]

    def write_snapshot(self, destination: Path) -> None:
        """Write the previously collected evidence into a private directory."""
        ...


def model_name(value: str) -> str:
    """Accept model aliases and provider IDs without flags or control characters."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}", value):
        raise argparse.ArgumentTypeError(
            "Use a model alias or provider ID without spaces, control characters, or a leading dash (maximum 256 characters)."
        )
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


def compose_prompt(evidence: str, focus: str, metadata: dict[str, Any], target: str) -> bytes:
    """Combine the installed review instructions with explicitly scoped evidence."""
    template = (PLUGIN_ROOT / "prompts" / "adversarial-review.md").read_text(encoding="utf-8")
    values = {
        "TARGET": target,
        "FOCUS": focus,
        "SCOPE": json.dumps(metadata, ensure_ascii=False),
        "EVIDENCE": evidence,
    }
    prompt = re.sub(
        r"\{\{(TARGET|FOCUS|SCOPE|EVIDENCE)\}\}",
        lambda match: values[match.group(1)],
        template,
    ).encode("utf-8")
    if len(prompt) > MAX_PROMPT_BYTES:
        raise ValueError("Composed prompt exceeds 512 KiB; narrow the paths or reduce the prompt packet.")
    return prompt


def command(model: str, *, repository: bool = False) -> list[str]:
    """Confine source inspection to a snapshot and disable executable tools."""
    schema = json.loads((PLUGIN_ROOT / "schemas" / "review-output.schema.json").read_text(encoding="utf-8"))
    argv = [
        "claude",
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "Read,Glob,Grep" if repository else "",
        "--disallowedTools",
        "mcp__*",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--print",
    ]
    if repository:
        argv.extend(["--allowedTools", "Read,Glob,Grep"])
    return argv


def validate_review(review: Any) -> str | None:
    """Validate the output contract without adding a JSON Schema dependency."""
    fields = {"verdict", "summary", "findings", "next_steps", "coverage_limitations"}
    if not isinstance(review, dict) or set(review) != fields:
        return "Review must contain exactly verdict, summary, findings, next_steps, and coverage_limitations."
    if review["verdict"] not in ("approve", "needs-attention", "insufficient-context"):
        return "Review verdict is invalid."
    if not isinstance(review["summary"], str) or not review["summary"].strip():
        return "Review summary must be nonempty text."
    for name in ("next_steps", "coverage_limitations"):
        if not isinstance(review[name], list) or any(not isinstance(item, str) or not item.strip() for item in review[name]):
            return f"Review {name} must be a list of nonempty strings."
    if not isinstance(review["findings"], list):
        return "Review findings must be a list."
    finding_fields = {
        "severity",
        "title",
        "body",
        "file",
        "line_start",
        "line_end",
        "confidence",
        "recommendation",
    }
    for finding in review["findings"]:
        if not isinstance(finding, dict) or set(finding) != finding_fields:
            return "Each finding must match the required fields exactly."
        if finding["severity"] not in ("critical", "high", "medium", "low"):
            return "Finding severity is invalid."
        for name in ("title", "body", "file", "recommendation"):
            if not isinstance(finding[name], str) or not finding[name].strip():
                return f"Finding {name} must be nonempty text."
        path = finding["file"]
        if (
            path in (".", "..")
            or path.startswith(("/", "~"))
            or "\\" in path
            or re.match(r"^[A-Za-z]:", path)
            or ".." in PurePosixPath(path).parts
            or any(ord(c) < 32 for c in path)
        ):
            return "Finding file must be a repository-relative path without traversal or control characters."
        if any(type(finding[name]) is not int or not 1 <= finding[name] <= 2147483647 for name in ("line_start", "line_end")):
            return "Finding line numbers must be positive integers."
        if finding["line_end"] < finding["line_start"]:
            return "Finding line range is reversed."
        confidence = finding["confidence"]
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence):
            return "Finding confidence must be a finite number from 0 to 1."
    if review["verdict"] == "approve" and review["findings"]:
        return "An approve verdict cannot contain findings."
    if review["verdict"] == "insufficient-context" and not review["coverage_limitations"]:
        return "An insufficient-context verdict must explain its coverage limitations."
    return None


def assess_response(raw: bytes, returncode: int) -> tuple[dict[str, Any], str | None]:
    """Separate transport success from a schema-valid substantive verdict."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return (
            {},
            "Claude exited unsuccessfully." if returncode != 0 else "Claude returned malformed JSON.",
        )
    if not isinstance(payload, dict):
        return (
            {},
            "Claude exited unsuccessfully." if returncode != 0 else "Claude returned an unexpected JSON shape.",
        )
    if returncode != 0:
        return payload, "Claude exited unsuccessfully."
    if payload.get("type") != "result" or payload.get("subtype") != "success" or payload.get("is_error") is not False:
        return payload, "Claude did not report a successful result."
    error = validate_review(payload.get("structured_output"))
    return payload, error


def format_review(review: dict[str, Any]) -> str:
    """Render findings without rewriting or filtering their original content."""
    lines = [
        "# Adversarial review",
        "",
        f"Verdict: **{review['verdict']}**",
        "",
        review["summary"],
        "",
    ]
    for index, finding in enumerate(review["findings"], 1):
        lines.extend(
            [
                f"## {index}. [{finding['severity']}] {finding['title']}",
                "",
                f"File: `{finding['file']}:{finding['line_start']}-{finding['line_end']}` · Confidence: {finding['confidence']}",
                "",
                finding["body"],
                "",
                "Recommendation:",
                "",
                finding["recommendation"],
                "",
            ]
        )
    for title, key in (
        ("Coverage limitations", "coverage_limitations"),
        ("Next steps", "next_steps"),
    ):
        lines.extend([f"## {title}", ""])
        lines.extend([f"- {item}" for item in review[key]] or ["None reported."])
        lines.append("")
    return "\n".join(lines)


def check_cli(cwd: Path) -> str | None:
    """Fail closed if the installed CLI lacks the required confinement flags."""
    try:
        result = subprocess.run(
            ["claude", "--help"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Claude CLI preflight failed; verify the installed CLI is on PATH and responds to --help."
    help_text = result.stdout.decode("utf-8", errors="replace")
    required = (
        "--safe-mode",
        "--restricted",
        "--tools",
        "--disallowedTools",
        "--json-schema",
        "--strict-mcp-config",
        "--no-session-persistence",
    )
    missing = [flag for flag in required if flag not in help_text]
    if result.returncode != 0 or missing:
        return f"Claude CLI lacks required review capabilities ({', '.join(missing) or 'help failed'}). Install Claude Code 2.1.248 or newer; confinement is never disabled as a fallback."
    return None


def _terminate(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Stop this invocation and its process group before collecting diagnostics."""
    for force in (False, True):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif not force:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            continue
    return b"", b"Process output was unavailable after termination."


def _interrupt(signum: int, frame: FrameType | None) -> None:
    """Route termination through the artifact and child cleanup path."""
    del signum, frame
    raise KeyboardInterrupt


def run_review(
    packet: bytes,
    model: str,
    timeout: float,
    output_parent: Path | None,
    *,
    context: ReviewContext | None = None,
) -> tuple[Path, int]:
    """Retain private artifacts and distinguish execution failure from review findings."""
    output = Path(tempfile.mkdtemp(prefix="claude-adversarial-review-", dir=output_parent))
    snapshot = output / "context"
    snapshot.mkdir(mode=0o700)
    argv = command(model, repository=context is not None)
    context_metadata = context.metadata if context else {"mode": "packet-only"}
    metadata: dict[str, Any] = {
        "status": "preparing",
        "execution_status": "pending",
        "error": None,
        "requested_model": model,
        "actual_models": [],
        "prompt_bytes": len(packet),
        "timeout_seconds": timeout,
        "returncode": None,
        "command": argv,
        "context": context_metadata,
        "review_verdict": None,
        "effective_verdict": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    def save_metadata() -> None:
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    save_metadata()
    print(f"Review artifacts: {output}", flush=True)
    payload: dict[str, Any] = {}
    raw, stderr = b"", b""
    error: str | None = None
    interrupted = False
    start = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    previous_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if context:
            context.write_snapshot(snapshot)
        error = check_cli(snapshot)
        if error is None:
            metadata.update(status="running", execution_status="running")
            save_metadata()
            process = subprocess.Popen(
                argv,
                cwd=snapshot,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            metadata["pid"] = process.pid
            save_metadata()
            try:
                raw, stderr = process.communicate(input=packet, timeout=timeout)
            except subprocess.TimeoutExpired:
                raw, stderr = _terminate(process)
                error = "Claude exceeded the review timeout."
            except KeyboardInterrupt:
                raw, stderr = _terminate(process)
                interrupted = True
                error = "Claude review was interrupted."
            metadata["returncode"] = process.returncode
            if error is None:
                payload, error = assess_response(raw, process.returncode if process.returncode is not None else 1)
    except KeyboardInterrupt:
        interrupted = True
        error = "Claude review was interrupted."
    except OSError:
        error = "Claude or its private snapshot could not be prepared; verify CLI availability and filesystem permissions."
    except ValueError as exc:
        error = str(exc)
    finally:
        if process is not None and process.poll() is None:
            raw, stderr = _terminate(process)
            metadata["returncode"] = process.returncode
        signal.signal(signal.SIGTERM, previous_handler)
    (output / "response.json").write_bytes(raw)
    (output / "stderr.log").write_bytes(stderr)
    review = payload.get("structured_output")
    if isinstance(review, dict):
        (output / "review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if error:
        markdown = f"# Review incomplete\n\n{error}\n"
        status, execution_status, exit_code = (
            "failed",
            "interrupted" if interrupted else "failed",
            130 if interrupted else 1,
        )
    else:
        review = payload["structured_output"]
        verdict = review["verdict"]
        host_incomplete = context_metadata.get("coverage_complete") is False or context_metadata.get("changes") == []
        incomplete = verdict == "insufficient-context" or host_incomplete
        metadata.update(
            review_verdict=verdict,
            effective_verdict="insufficient-context" if incomplete else verdict,
        )
        markdown = format_review(review)
        if host_incomplete:
            markdown += "\n## Host coverage limitation\n\nSelected change evidence is incomplete or empty in the snapshot. The effective outcome is insufficient-context. See metadata.json for the exact omissions.\n"
        status, execution_status, exit_code = (
            "insufficient-context" if incomplete else "success",
            "succeeded",
            2 if incomplete else 0,
        )
    (output / "review.md").write_text(markdown, encoding="utf-8")
    model_usage = payload.get("modelUsage", {})
    metadata.update(
        status=status,
        execution_status=execution_status,
        error=error,
        actual_models=sorted(model_usage) if isinstance(model_usage, dict) else [],
        duration_seconds=round(time.monotonic() - start, 3),
        finished_at=datetime.now(timezone.utc).isoformat(),
        exit_code=exit_code,
    )
    save_metadata()
    return output, exit_code


def main(argv: list[str] | None = None) -> int:
    """Prepare a scope-aware review or validate its inventory without model calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="Explicit packet-only mode: inspected UTF-8 material (maximum 512 KiB).",
    )
    parser.add_argument("--repo", type=Path, help="Repository to review (default: current directory).")
    parser.add_argument(
        "--scope",
        choices=("auto", "working-tree", "branch"),
        help="Change scope (default: auto).",
    )
    parser.add_argument("--base", help="Base revision for a branch review.")
    parser.add_argument(
        "--path",
        action="append",
        help="Changed target path filter; repeat to select multiple paths.",
    )
    parser.add_argument("--exclude", action="append", help="Additional excluded glob; repeat as needed.")
    parser.add_argument("--focus", default=DEFAULT_FOCUS, help="Review focus or selected lenses.")
    parser.add_argument("--model", type=model_name, default="opus")
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=300.0,
        help="Deadline in seconds (default: 300).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Existing parent for a unique private run directory (default: system temp).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show scope, inventory, exclusions and flags without file bodies, model calls, or artifact writes.",
    )
    args = parser.parse_args(argv)
    if args.prompt_file and any(value is not None for value in (args.repo, args.scope, args.base, args.path, args.exclude)):
        parser.error("--prompt-file cannot be combined with --repo, --scope, --base, --path, or --exclude.")
    context: ReviewContext | None = None
    try:
        if args.output_dir is not None and not args.output_dir.is_dir():
            raise ValueError("Output parent must be an existing directory.")
        if args.prompt_file:
            evidence = read_packet(args.prompt_file).decode("utf-8")
            metadata = {
                "mode": "packet-only",
                "input_bytes": len(evidence.encode("utf-8")),
            }
            target = "Explicit prompt packet; source file tools are unavailable."
        else:
            sys.dont_write_bytecode = True
            from review_context import collect_context

            context = collect_context(
                args.repo or Path.cwd(),
                scope=args.scope or "auto",
                base=args.base,
                paths=args.path,
                excludes=args.exclude,
            )
            evidence, metadata = context.prompt, context.metadata
            target = "Repository change evidence in the private context snapshot."
        packet = compose_prompt(evidence, args.focus, metadata, target)
        argv_preview = command(args.model, repository=context is not None)
    except (OSError, ValueError) as exc:
        parser.error(str(exc) if isinstance(exc, ValueError) else "Could not read the input, installed plugin resources, or output parent.")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "command": argv_preview,
                    "prompt_bytes": len(packet),
                    "context": metadata,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    try:
        output, exit_code = run_review(packet, args.model, args.timeout, args.output_dir, context=context)
    except OSError:
        print("Could not create or write private review artifacts.", file=sys.stderr)
        return 1
    final = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    print(f"Review execution: {final['execution_status']}; outcome: {final['effective_verdict'] or final['status']}. Artifacts: {output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
