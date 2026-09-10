#!/usr/bin/env python3
"""Run an independent Claude review of live Git changes, an explicit snapshot, or a prompt packet."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import FrameType, ModuleType
from typing import Any, NoReturn, Protocol, cast

MAX_PROMPT_BYTES = 512 * 1024
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SETTINGS_ENV = "CLAUDE_ADVERSARIAL_REVIEW_SANDBOX_SETTINGS"
DEFAULT_FOCUS = (
    "security, performance, code correctness, frontend and accessibility, architecture, reliability, privacy, compatibility, testing, operations"
)
HOST_ACCESS_NOTICE = (
    "Host launch runs the review runner, Git collection, and Claude outside Codex's sandbox. "
    "When enabled, Claude's sandbox covers Bash tool subprocesses, not the entire Claude process. "
    "The calling host must authorize this boundary; the runner cannot change or verify its enclosing sandbox."
)


class ReviewContext(Protocol):
    """Resolved review scope and mode-specific change evidence."""

    prompt: str
    metadata: dict[str, Any]

    def write_snapshot(self, destination: Path) -> None:
        """Write the previously collected evidence into a private directory."""
        ...

    def recheck(self) -> dict[str, Any]:
        """Check whether selected live evidence still matches its captured fingerprint."""
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


class ReviewArgumentParser(argparse.ArgumentParser):
    """Reserve exit 64 for invalid usage, distinct from completed review outcomes."""

    def error(self, message: str) -> NoReturn:
        """Print ordinary argparse diagnostics with a dedicated usage status."""
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def compact_scope(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep repository-sized inventories outside the model's initial prompt."""
    if metadata.get("mode") == "packet-only":
        return {"mode": "packet-only", "input_bytes": metadata.get("input_bytes")}
    keys = (
        "context_mode",
        "repo_root",
        "git_commands",
        "scope",
        "requested_scope",
        "head_commit",
        "base_ref",
        "base_commit",
        "merge_base",
        "source_revision",
        "selected_count",
        "source_file_count",
        "source_bytes",
        "diff_bytes",
        "coverage_complete",
        "supporting_context_complete",
        "worktree_dirty",
        "input_mode",
        "changed_targets_outside_path_filters",
        "excluded_change_count",
        "exclusions_are_access_boundary",
    )
    summary = {key: metadata[key] for key in keys if key in metadata}
    if metadata.get("context_mode") == "live":
        excluded_counts: dict[str, int] = {}
        for change in metadata.get("excluded_changes", []):
            reason = change["reason"]
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
        summary["excluded_change_reasons"] = excluded_counts
    counts = metadata.get("omission_counts")
    if isinstance(counts, dict):
        summary["omission_counts"] = {key: counts[key] for key in ("total", "selected", "supporting") if key in counts}
    else:
        omissions = metadata.get("omissions", [])
        selected = sum(isinstance(item, dict) and item.get("selected_target") is True for item in omissions)
        summary["omission_counts"] = {"total": len(omissions), "selected": selected, "supporting": len(omissions) - selected}
    if metadata.get("context_mode") != "live":
        summary["inventory"] = "Read inventory.json for source paths, selected omissions, and supporting omission summaries."
    return summary


def installed_module(name: str) -> ModuleType:
    """Load trusted installed siblings without consulting the target repository or Python search path."""
    module_path = PLUGIN_ROOT / "scripts" / f"{name}.py"
    if not module_path.is_file():
        raise ValueError(f"Installed plugin is incomplete: scripts/{name}.py is missing. Reinstall the complete plugin, not review.py alone.")
    module_name = f"_claude_adversarial_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None) == str(module_path):
        return cached
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load installed scripts/{name}.py; reinstall the complete plugin.")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError) as exc:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        raise ValueError(f"Could not load installed scripts/{name}.py; reinstall the complete plugin.") from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode
    return module


def collect_repository_context(
    repo: Path,
    *,
    scope: str,
    base: str | None,
    paths: list[str] | None,
    excludes: list[str] | None,
    context_mode: str = "live",
) -> ReviewContext:
    """Resolve repository evidence using the collector distributed alongside this runner."""
    collector = getattr(installed_module("review_context"), "collect_context", None)
    if not callable(collector):
        raise ValueError("Installed context collector is incompatible; reinstall the complete plugin.")
    context = cast(ReviewContext, collector(repo, scope=scope, base=base, paths=paths, excludes=excludes, context_mode=context_mode))
    if context.metadata.get("context_mode") == "live":
        runtime = installed_module("claude_runtime")
        try:
            context.metadata["git_commands"] = runtime.live_git_commands(Path(context.metadata["repo_root"]), **live_scope(context.metadata))
        except runtime.RuntimeFailure as exc:
            raise ValueError(str(exc)) from exc
    return context


def compose_prompt(evidence: str, focus: str, metadata: dict[str, Any], target: str) -> bytes:
    """Combine scoped evidence with distinct, unpredictable boundary markers."""
    template = (PLUGIN_ROOT / "prompts" / "adversarial-review.md").read_text(encoding="utf-8")
    values = {
        "TARGET": target,
        "FOCUS": focus if focus.strip() else DEFAULT_FOCUS,
        "SCOPE": json.dumps(compact_scope(metadata), ensure_ascii=False),
        "EVIDENCE": evidence,
    }
    while True:
        token = secrets.token_hex(16)
        opening, closing = f"<review-evidence-{token}>", f"</review-evidence-{token}>"
        if not any(opening in value or closing in value for value in (*values.values(), template)):
            break
    values.update(EVIDENCE_OPEN=opening, EVIDENCE_CLOSE=closing)
    prompt = re.sub(
        r"\{\{(TARGET|FOCUS|SCOPE|EVIDENCE|EVIDENCE_OPEN|EVIDENCE_CLOSE)\}\}",
        lambda match: values[match.group(1)],
        template,
    ).encode("utf-8")
    if len(prompt) > MAX_PROMPT_BYTES:
        raise ValueError("Composed prompt exceeds 512 KiB; reduce the selected inline evidence, focus text, or explicit prompt packet.")
    return prompt


def live_scope(metadata: dict[str, Any]) -> dict[str, Any]:
    """Pass the captured comparison to policy and canonical command generation."""
    return {key: metadata.get(key) for key in ("scope", "head_commit", "merge_base")}


def live_policy(
    context: ReviewContext, sandbox_settings: dict[str, Any] | None = None, *, host_sandbox_mode: str = "unknown",
) -> dict[str, Any]:
    """Build the required live-tool restrictions using captured Git directory locations."""
    metadata = context.metadata
    runtime = installed_module("claude_runtime")
    try:
        return cast(
            dict[str, Any],
            runtime.build_live_policy(
                Path(metadata["repo_root"]),
                [Path(metadata["git_dir"]), Path(metadata["git_common_dir"])],
                sandbox_settings=sandbox_settings,
                host_sandbox_mode=host_sandbox_mode,
                **live_scope(metadata),
            ),
        )
    except runtime.RuntimeFailure as exc:
        raise ValueError(str(exc)) from exc


def command(
    model: str, *, repository: bool = False, context: ReviewContext | None = None, policy: dict[str, Any] | None = None,
) -> list[str]:
    """Select live inspection, explicit snapshot reads, or tool-free packet review."""
    schema = json.loads((PLUGIN_ROOT / "schemas" / "review-output.schema.json").read_text(encoding="utf-8"))
    if context is not None and context.metadata.get("context_mode") == "live":
        argv = installed_module("claude_runtime").live_cli_arguments(policy if policy is not None else live_policy(context), Path(context.metadata["repo_root"]))
        return [*argv, "--model", model, "--json-schema", json.dumps(schema)]
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


def check_cli(cwd: Path, *, live: bool = False) -> str | None:
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
    if live:
        required += ("--input-format", "--output-format", "--settings", "--add-dir", "--permission-mode")
    missing = [flag for flag in required if flag not in help_text]
    if result.returncode != 0 or missing:
        return (
            f"Claude CLI lacks required review capabilities ({', '.join(missing) or 'help failed'}). "
            "Update Claude Code and retry; capabilities are checked at runtime and confinement is never disabled as a fallback."
        )
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
    sandbox_settings: dict[str, Any] | None = None,
    sandbox_settings_source: str | None = None,
    host_sandbox_mode: str = "unknown",
) -> tuple[Path, int]:
    """Retain private artifacts and distinguish execution failure from review findings."""
    is_live = context is not None and context.metadata.get("context_mode") == "live"
    if sandbox_settings is not None and not is_live:
        raise ValueError("Sandbox settings apply only to live repository reviews.")
    policy = live_policy(context, sandbox_settings, host_sandbox_mode=host_sandbox_mode) if is_live and context is not None else None
    argv = command(model, repository=context is not None, context=context, policy=policy)
    cli_error = check_cli(PLUGIN_ROOT, live=is_live)
    if cli_error:
        raise ValueError(cli_error)
    output = Path(tempfile.mkdtemp(prefix="claude-adversarial-review-", dir=output_parent))
    snapshot = output / "context"
    if not is_live:
        snapshot.mkdir(mode=0o700)
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
        "sandbox_settings_source": (
            sandbox_settings_source or ("provided settings object" if sandbox_settings is not None else "bundled settings/sandbox.json")
        ) if is_live else None,
        "execution_boundary": "Chosen by the calling host; enclosing sandbox not verified by the runner.",
        "declared_host_sandbox_mode": host_sandbox_mode,
        "host_mode_verified": False,
        "requested_sandbox_enabled": policy["sandbox"]["enabled"] if policy is not None else None,
        "claude_sandbox_scope": "Bash tool subprocesses (when enabled)" if is_live else "No Bash tools granted",
        "context": context_metadata,
        "review_verdict": None,
        "effective_verdict": None,
        "started_at": datetime.now(UTC).isoformat(),
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
        if context and not is_live:
            context.write_snapshot(snapshot)
        if is_live and context is not None and policy is not None:
            metadata.update(status="running", execution_status="running")
            save_metadata()

            def record_pid(pid: int) -> None:
                metadata["pid"] = pid
                save_metadata()

            with tempfile.TemporaryDirectory(prefix="claude-review-launcher-") as launcher_name:
                result = installed_module("claude_runtime").run_live(
                    argv,
                    packet,
                    Path(launcher_name),
                    policy,
                    timeout,
                    output / "events.jsonl",
                    output / "stderr.log",
                    on_started=record_pid,
                )
            metadata["returncode"] = result.returncode
            metadata["policy_verification"] = result.policy_summary
            interrupted = result.interrupted
            raw = json.dumps(result.payload, ensure_ascii=False).encode("utf-8") if result.payload else b""
            payload, response_error = assess_response(raw, result.returncode if result.returncode is not None else 1)
            error = result.error or response_error
            if error is None:
                try:
                    stability = context.recheck()
                except (OSError, ValueError):
                    stability = {"unchanged": False, "reason": "Selected evidence could not be rechecked after review."}
                metadata["scope_stability"] = stability
        else:
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
        error = "Claude or its private runtime could not be prepared; verify CLI availability and filesystem permissions."
    except ValueError as exc:
        error = str(exc)
    finally:
        if process is not None and process.poll() is None:
            raw, stderr = _terminate(process)
            metadata["returncode"] = process.returncode
        signal.signal(signal.SIGTERM, previous_handler)
    (output / "response.json").write_bytes(raw)
    if not is_live or not (output / "stderr.log").exists():
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
        stale = is_live and metadata.get("scope_stability", {}).get("unchanged") is not True
        host_incomplete = host_incomplete or stale
        incomplete = verdict == "insufficient-context" or host_incomplete
        metadata.update(
            review_verdict=verdict,
            effective_verdict="insufficient-context" if incomplete else verdict,
        )
        markdown = format_review(review)
        if host_incomplete:
            markdown += (
                "\n## Host coverage limitation\n\n"
                + (
                    "The selected scope changed during review or could not be verified again. "
                    if stale
                    else "Selected change evidence is incomplete or empty. "
                )
                + "The effective outcome is insufficient-context. See metadata.json for details.\n"
            )
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
        finished_at=datetime.now(UTC).isoformat(),
        exit_code=exit_code,
    )
    save_metadata()
    return output, exit_code


def main(argv: list[str] | None = None) -> int:
    """Prepare a scope-aware review or validate its inventory without model calls."""
    parser = ReviewArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="Explicit packet-only mode: inspected UTF-8 material (maximum 512 KiB).",
    )
    parser.add_argument("--repo", type=Path, help="Repository to review (default: current directory).")
    parser.add_argument(
        "--context-mode",
        choices=("live", "snapshot"),
        help="Repository evidence access: live files and Git (default), or an explicit bounded source snapshot.",
    )
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
    parser.add_argument(
        "--exclude",
        action="append",
        help=(
            "Excluded scope glob; repeat as needed. Live exclusions do not restrict file or Git access; snapshot exclusions also omit source copies."
        ),
    )
    parser.add_argument("--focus", default=DEFAULT_FOCUS, help="Review focus or selected lenses.")
    parser.add_argument("--model", type=model_name, default="opus")
    parser.add_argument(
        "--sandbox-settings", type=Path,
        help=f"Live mode only: JSON overlay; overrides {SANDBOX_SETTINGS_ENV}. Mandatory review protections remain enabled.",
    )
    parser.add_argument(
        "--host-sandbox-mode", choices=("restricted", "full-access", "unknown"), default="unknown",
        help="Original calling task mode, retained across host launch. Only full-access permits disabling the live Bash sandbox; unknown requires it.",
    )
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
    if args.prompt_file and any(value is not None for value in (args.repo, args.scope, args.base, args.path, args.exclude, args.context_mode)):
        parser.error("--prompt-file cannot be combined with --repo, --scope, --base, --path, --exclude, or --context-mode.")
    if args.sandbox_settings is not None and (args.prompt_file is not None or args.context_mode == "snapshot"):
        parser.error("--sandbox-settings is only supported with live repository reviews.")
    print(f"Execution boundary notice: {HOST_ACCESS_NOTICE}", file=sys.stderr)
    context: ReviewContext | None = None
    sandbox_settings: dict[str, Any] | None = None
    settings_source = "bundled settings/sandbox.json"
    try:
        if args.prompt_file is None and args.context_mode != "snapshot" and args.sandbox_settings is None:
            configured_path = os.environ.get(SANDBOX_SETTINGS_ENV)
            if configured_path:
                if not Path(configured_path).is_absolute() and not configured_path.startswith("~/"):
                    raise ValueError(f"{SANDBOX_SETTINGS_ENV} must name an absolute or ~/ settings file path.")
                args.sandbox_settings = Path(configured_path)
        if args.sandbox_settings is not None:
            try:
                args.sandbox_settings = args.sandbox_settings.expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("The sandbox settings file path could not be resolved.") from exc
            settings_source = str(args.sandbox_settings)
        if args.prompt_file is None and args.context_mode != "snapshot":
            sandbox_settings = installed_module("claude_runtime").load_sandbox_settings(args.sandbox_settings, host_sandbox_mode=args.host_sandbox_mode)
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
            context = collect_repository_context(
                args.repo or Path.cwd(),
                scope=args.scope or "auto",
                base=args.base,
                paths=args.path,
                excludes=args.exclude,
                context_mode=args.context_mode or "live",
            )
            evidence, metadata = context.prompt, context.metadata
            target = (
                "Live repository files and captured Git changes; exclusions define scope, not an access boundary."
                if args.context_mode != "snapshot"
                else "Repository change evidence in the private context snapshot."
            )
        packet = compose_prompt(evidence, args.focus, metadata, target)
        policy = live_policy(context, sandbox_settings, host_sandbox_mode=args.host_sandbox_mode) if sandbox_settings is not None and context is not None else None
        argv_preview = command(args.model, repository=context is not None, context=context, policy=policy)
    except (OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "Could not read the input, installed plugin resources, or output parent."
        print(f"Review preflight failed: {detail} No review artifacts were created.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Review interrupted before execution. No review artifacts were created.", file=sys.stderr)
        return 130
    if args.dry_run:
        print(
            json.dumps(
                {
                    "command": argv_preview,
                    "prompt_bytes": len(packet),
                    "context": metadata,
                    "sandbox_settings_source": settings_source if sandbox_settings is not None else None,
                    "declared_host_sandbox_mode": args.host_sandbox_mode,
                    "host_mode_verified": False,
                    "requested_sandbox_enabled": policy["sandbox"]["enabled"] if policy is not None else None,
                    "execution_boundary_notice": HOST_ACCESS_NOTICE,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    try:
        output, exit_code = run_review(
            packet, args.model, args.timeout, args.output_dir, context=context,
            sandbox_settings=sandbox_settings, sandbox_settings_source=settings_source,
            host_sandbox_mode=args.host_sandbox_mode,
        )
    except ValueError as exc:
        print(f"Review failed: {exc} If an artifact path was printed, inspect that directory.", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Could not create or write private review artifacts. If an artifact path was printed, partial artifacts may remain there.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Review interrupted. If an artifact path was printed, partial artifacts may remain there.", file=sys.stderr)
        return 130
    final = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    print(f"Review execution: {final['execution_status']}; outcome: {final['effective_verdict'] or final['status']}. Artifacts: {output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
