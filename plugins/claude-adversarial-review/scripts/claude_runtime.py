"""Drive a live repository review after checking the CLI's effective policy."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 512 * 1024
READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
HOST_SANDBOX_MODES = ("restricted", "full-access", "unknown")
DEFAULT_SANDBOX_SETTINGS = Path(__file__).resolve().parents[1] / "settings" / "sandbox.json"
REQUIRED_SANDBOX = {
    "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
    "autoAllowBashIfSandboxed": False, "excludedCommands": [],
    "enableWeakerNestedSandbox": False, "enableWeakerNetworkIsolation": False,
    "allowAppleEvents": False,
}
SANDBOX_FIELDS = {
    "sandbox": {
        **{key: type(value) for key, value in REQUIRED_SANDBOX.items()},
        "filesystem": {
            "disabled": bool, "allowRead": list, "denyRead": list, "allowWrite": list, "denyWrite": list,
        },
        "network": {
            "allowedDomains": list, "deniedDomains": list, "strictAllowlist": bool,
            "allowAllUnixSockets": bool, "allowUnixSockets": list, "allowLocalBinding": bool, "allowMachLookup": list,
        },
    },
}
REQUIRED_DENIES = (
    "Cd",
    "Edit",
    "Write",
    "NotebookEdit",
    "mcp__*",
    "Bash(cd)",
    "Bash(cd *)",
    "Bash(chdir)",
    "Bash(chdir *)",
    "Bash(pushd)",
    "Bash(pushd *)",
    "Bash(popd)",
    "Bash(popd *)",
)


class RuntimeFailure(Exception):
    """A safe diagnostic that contains no model or configuration content."""


@dataclass
class LiveResult:
    """Execution state, separate from the model's structured review verdict."""

    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    returncode: int = 1
    interrupted: bool = False
    timed_out: bool = False
    policy_summary: dict[str, Any] = field(default_factory=dict)


def live_git_commands(
    repo: Path,
    *,
    scope: str = "working-tree",
    head_commit: str | None = None,
    merge_base: str | None = None,
) -> dict[str, list[str]]:
    """Describe narrowly approved Git commands and option-terminated operands."""
    repository = str(repo.resolve())
    if "*" in repository or any(ord(character) < 32 or ord(character) == 127 for character in repository):
        raise ValueError("The repository path contains characters that cannot be safely represented in Git permission rules.")
    for revision in (head_commit, merge_base):
        if revision is not None and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None:
            raise ValueError("Git review commands require full captured commit IDs.")
    if scope not in {"working-tree", "branch"}:
        raise ValueError("Git review commands require a resolved working-tree or branch scope.")
    if scope == "branch" and (head_commit is None or merge_base is None):
        raise ValueError("Branch Git commands require captured head and merge-base commits.")
    prefix = [
        "git",
        "--no-pager",
        "--no-optional-locks",
        "--literal-pathspecs",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-C",
        repository,
    ]
    exact = [shlex.join([*prefix, "status", "--porcelain=v1", "--ignore-submodules=none"])]
    operands = [shlex.join([*prefix, "cat-file", "blob", "--"])]
    if scope == "working-tree":
        exact.extend(shlex.join([*prefix, "ls-files", *flags]) for flags in (("--others", "--exclude-standard"), ("--cached",), ("--stage",)))
        comparisons: list[list[str]] = [[]]
        if head_commit is not None:
            comparisons.append(["--cached", head_commit])
    else:
        assert merge_base is not None and head_commit is not None
        comparisons = [[merge_base, head_commit]]
    for comparison in comparisons:
        diff = [
            *prefix,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--submodule=short",
            "--ignore-submodules=none",
            *comparison,
        ]
        exact.append(shlex.join([*diff, "--name-status", "--"]))
        command = shlex.join([*diff, "--"])
        exact.append(command)
        operands.append(command)
    for revision in dict.fromkeys(value for value in (head_commit, merge_base) if value is not None):
        exact.append(shlex.join([*prefix, "ls-tree", "-r", "--name-only", revision, "--"]))
        command = shlex.join([*prefix, "log", "--no-show-signature", "--format=fuller", "-n", "10", revision, "--"])
        exact.append(command)
        operands.append(command)
    return {"exact": exact, "operand_prefixes": operands}


def _validate_sandbox_requirements(sandbox: dict[str, Any], host_sandbox_mode: str) -> None:
    if host_sandbox_mode not in HOST_SANDBOX_MODES:
        raise ValueError("Unsupported originating host sandbox mode.")
    if type(sandbox.get("enabled")) is not bool or (host_sandbox_mode != "full-access" and not sandbox["enabled"]):
        raise ValueError("Bash sandboxing is required unless the originating host task is explicitly declared full-access.")
    if any(sandbox.get(key) != value for key, value in REQUIRED_SANDBOX.items() if key != "enabled") or sandbox["filesystem"].get("disabled") is not False:
        raise ValueError("Custom settings must preserve fixed sandbox fallback and review-only tool restrictions.")


def load_sandbox_settings(override: Path | None = None, *, host_sandbox_mode: str = "unknown") -> dict[str, Any]:
    """Read one explicit overlay; never discover settings in the reviewed checkout."""
    def read(path: Path) -> dict[str, Any]:
        try:
            descriptor = os.open(path.expanduser(), os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError
            settings = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(settings, dict) or set(settings) != {"sandbox"} or not isinstance(settings["sandbox"], dict):
                raise ValueError
            return settings
        except (OSError, ValueError, RuntimeFailure, RecursionError) as exc:
            raise ValueError("Sandbox settings must be a regular UTF-8 JSON file of at most 64 KiB containing only a sandbox object.") from exc

    def merge(base: dict[str, Any], changes: Any, fields: dict[str, Any], *, complete: bool = False) -> None:
        if not isinstance(changes, dict) or not changes.keys() <= fields.keys() or (complete and changes.keys() != fields.keys()):
            raise ValueError("Sandbox settings contain an unsupported section or field; use the bundled settings/sandbox.json as the template.")
        for key, value in changes.items():
            expected = fields[key]
            if isinstance(expected, dict):
                merge(base.setdefault(key, {}), value, expected, complete=complete)
            elif type(value) is not expected or (
                isinstance(value, list)
                and any(not isinstance(item, str) or not item or any(ord(c) < 32 or ord(c) == 127 for c in item) for item in value)
            ):
                raise ValueError("Sandbox settings require booleans and lists of nonempty strings without control characters.")
            else:
                base[key] = value

    settings: dict[str, Any] = {}
    merge(settings, read(DEFAULT_SANDBOX_SETTINGS), SANDBOX_FIELDS, complete=True)
    if override is not None:
        merge(settings, read(override), SANDBOX_FIELDS)
    sandbox = settings["sandbox"]
    _validate_sandbox_requirements(sandbox, host_sandbox_mode)
    for section, names in ((sandbox["filesystem"], ("allowRead", "denyRead", "allowWrite", "denyWrite")), (sandbox["network"], ("allowUnixSockets",))):
        for name in names:
            paths = []
            for value in section[name]:
                try:
                    path = Path(value).expanduser()
                    if not path.is_absolute() or ("Write" in name and any(c in value for c in "*?[]{}")):
                        raise ValueError
                    paths.append(str(path.resolve()))
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError("Sandbox paths must resolve to absolute or ~/ paths; write paths cannot contain wildcards.") from exc
            section[name] = list(dict.fromkeys(paths))
    return settings


def build_live_policy(
    repo: Path,
    git_dirs: Sequence[Path],
    *,
    scope: str = "working-tree",
    head_commit: str | None = None,
    merge_base: str | None = None,
    sandbox_settings: dict[str, Any] | None = None,
    host_sandbox_mode: str = "unknown",
) -> dict[str, Any]:
    """Build a deterministic policy without launching processes or writing files."""
    repository = str(repo.resolve())
    roots = list(dict.fromkeys([repository, *(str(path.resolve()) for path in git_dirs)]))
    commands = live_git_commands(repo, scope=scope, head_commit=head_commit, merge_base=merge_base)
    approvals = [f"Bash({command})" for command in commands["exact"]]
    approvals.extend(f"Bash({prefix} *)" for prefix in commands["operand_prefixes"])
    sandbox = json.loads(json.dumps(sandbox_settings if sandbox_settings is not None else load_sandbox_settings()))["sandbox"]
    _validate_sandbox_requirements(sandbox, host_sandbox_mode)
    for key in ("denyWrite", "allowRead"):
        sandbox["filesystem"][key] = list(dict.fromkeys([*sandbox["filesystem"][key], *roots]))
    return {
        "permissions": {
            "allow": [*sorted(READ_TOOLS), *approvals],
            "deny": list(REQUIRED_DENIES),
            "additionalDirectories": [repository],
            "blockReadsOutsideWorkingDirectories": True,
        },
        "sandbox": sandbox,
    }


def live_cli_arguments(policy: dict[str, Any], repo: Path) -> list[str]:
    """Return the CLI invocation; callers append the model and output schema."""
    return [
        "claude",
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "Read,Glob,Grep,Bash",
        "--allowedTools",
        "Read,Glob,Grep",
        "--disallowedTools",
        "mcp__*",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--add-dir",
        str(repo.resolve()),
        "--settings",
        json.dumps(policy, sort_keys=True, separators=(",", ":")),
        "--print",
    ]


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeFailure(f"Claude returned an invalid {name} policy; review was not submitted.")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeFailure(f"Claude did not expose its effective {name} policy; review was not submitted.")
    return value


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "linux":
        try:
            if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower():
                return "wsl"
        except OSError:
            pass
        return "linux"
    raise RuntimeFailure("Live review is supported on macOS, Linux, or WSL2.")


def verify_effective_policy(response: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """Reject incompatible policy before exposing any review prompt to the CLI."""
    body = _mapping(response, "settings")
    if body.get("errors"):
        raise RuntimeFailure("Claude reported configuration errors; review was not submitted.")
    effective = _mapping(body.get("effective"), "settings")
    sandbox = _mapping(effective.get("sandbox"), "sandbox")
    expected = _mapping(policy.get("sandbox"), "requested sandbox")
    enabled = sandbox.get("enabled")
    if type(enabled) is not bool or (expected["enabled"] and not enabled):
        raise RuntimeFailure("Effective sandbox.enabled conflicts with the review policy; review was not submitted.")
    for key in (
        "failIfUnavailable",
        "allowUnsandboxedCommands",
        "autoAllowBashIfSandboxed",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
        "allowAppleEvents",
    ):
        if sandbox.get(key) is not expected[key]:
            raise RuntimeFailure(f"Effective sandbox.{key} conflicts with the review policy; review was not submitted.")
    platform = _platform()
    platforms = sandbox.get("enabledPlatforms")
    if enabled and platforms is not None and platform not in _string_list(platforms, "sandbox platforms"):
        raise RuntimeFailure("Managed policy disables the sandbox on this platform; review was not submitted.")
    if _string_list(sandbox.get("excludedCommands", []), "sandbox exclusions"):
        raise RuntimeFailure("Effective policy includes unsandboxed command exclusions; review was not submitted.")
    if sandbox.get("ripgrep") is not None:
        raise RuntimeFailure("Effective policy replaces the sandbox's search executable; review was not submitted.")
    filesystem = _mapping(sandbox.get("filesystem"), "filesystem")
    if filesystem.get("disabled") is not False:
        raise RuntimeFailure("Effective policy does not require filesystem isolation; review was not submitted.")
    writes = _string_list(filesystem.get("denyWrite"), "write restrictions")
    if not set(expected["filesystem"]["denyWrite"]).issubset(writes):
        raise RuntimeFailure("Effective policy does not protect the repository and Git metadata from writes; review was not submitted.")
    for key in ("allowWrite", "denyRead"):
        actual = set(_string_list(filesystem.get(key, []), key))
        requested = set(expected["filesystem"][key])
        if not (requested <= actual if key == "denyRead" else requested == actual):
            raise RuntimeFailure(f"Effective sandbox.filesystem.{key} conflicts with the review policy; review was not submitted.")
    reads = _string_list(filesystem.get("allowRead", []), "read grants")
    if set(reads) != set(expected["filesystem"]["allowRead"]):
        raise RuntimeFailure("Effective policy changes the review's filesystem read grants; review was not submitted.")
    network = _mapping(sandbox.get("network"), "network")
    for key in ("strictAllowlist", "allowAllUnixSockets", "allowLocalBinding"):
        if network.get(key) is not expected["network"][key]:
            raise RuntimeFailure(f"Effective sandbox.network.{key} conflicts with the review policy; review was not submitted.")
    for key in ("allowedDomains", "deniedDomains", "allowUnixSockets", "allowMachLookup"):
        actual = set(_string_list(network.get(key, []), key))
        requested = set(expected["network"][key])
        if not (requested <= actual if key == "deniedDomains" else requested == actual):
            raise RuntimeFailure(f"Effective sandbox.network.{key} conflicts with the review policy; review was not submitted.")
    if any(network.get(key) is not None for key in ("httpProxyPort", "socksProxyPort", "tlsTerminate")):
        raise RuntimeFailure("Effective policy replaces the sandbox network proxy; review was not submitted.")
    permissions = _mapping(effective.get("permissions"), "permissions")
    if permissions.get("blockReadsOutsideWorkingDirectories") is not True:
        raise RuntimeFailure("Effective policy permits reads outside the working directories; review was not submitted.")
    if not set(REQUIRED_DENIES).issubset(_string_list(permissions.get("deny"), "permission denials")):
        raise RuntimeFailure("Effective policy lost required tool or directory-change denials; review was not submitted.")
    if set(_string_list(permissions.get("allow", []), "permission grants")) != set(policy["permissions"]["allow"]):
        raise RuntimeFailure("Effective policy adds automatic tool approvals; review was not submitted.")
    directories = _string_list(permissions.get("additionalDirectories", []), "working directories")
    if set(directories) != set(policy["permissions"]["additionalDirectories"]):
        raise RuntimeFailure("Effective policy changes the additional working directories; review was not submitted.")
    if any(effective.get(key) for key in ("hooks", "statusLine", "fileSuggestion", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")):
        raise RuntimeFailure("Managed policy adds executable customizations; review was not submitted.")
    sources = body.get("sources")
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        raise RuntimeFailure("Claude did not expose configuration provenance; review was not submitted.")
    if any(source.get("errors") for source in sources):
        raise RuntimeFailure("Claude reported a configuration-source error; review was not submitted.")
    names = [source.get("source") for source in sources]
    if any(name not in {"flagSettings", "policySettings"} for name in names):
        raise RuntimeFailure("Claude loaded user or repository settings despite restricted mode; review was not submitted.")
    return {
        "verified": True,
        "verification_scope": "Claude-reported configuration",
        "os_enforcement_verified": False,
        "requested_sandbox_enabled": expected["enabled"],
        "configured_sandbox_enabled": enabled,
        "platform": platform,
        "sources": names,
        "write_deny_count": len(writes),
        "network": (
            "inactive: sandbox disabled" if not enabled else
            "subprocess destinations denied by configuration"
            if "*" in network.get("deniedDomains", [])
            and not any(network.get(key) for key in ("allowAllUnixSockets", "allowUnixSockets", "allowLocalBinding", "allowMachLookup"))
            else "custom subprocess network policy"
        ),
        "additional_write_grants": len(filesystem.get("allowWrite", [])),
        "filesystem_protection": "configured" if enabled else "inactive: sandbox disabled",
        "unsandboxed_commands": not enabled,
    }


def _environment(cwd: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            del environment[key]
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CEILING_DIRECTORIES=str(cwd.parent),
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="core.hooksPath",
        GIT_CONFIG_VALUE_0=os.devnull,
        GIT_CONFIG_KEY_1="core.fsmonitor",
        GIT_CONFIG_VALUE_1="false",
    )
    return environment


def _private_file(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Clean up an unfinished worker without signaling an already-reaped PID."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    # Keep the leader unreaped until all group signals have been sent: its PID
    # must not become eligible for reuse while it still identifies this group.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            continue
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        process.wait(timeout=2)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeFailure("Claude emitted a JSON record with duplicate keys.")
        result[key] = value
    return result


def run_live(
    argv: Sequence[str],
    prompt: bytes,
    cwd: Path,
    policy: dict[str, Any],
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    *,
    on_started: Callable[[int], None] | None = None,
) -> LiveResult:
    """Inspect effective policy, then submit one review and retain bounded events."""
    result = LiveResult()
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    try:
        _platform()
        if not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeFailure("Review timeout must be finite and positive.")
        if not prompt or len(prompt) > MAX_PROMPT_BYTES:
            raise RuntimeFailure("Review prompt is empty or exceeds the 512 KiB limit.")
        try:
            text = prompt.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeFailure("Review prompt must be UTF-8.") from exc
        cwd = cwd.resolve()
        if not cwd.is_dir() or any((parent / ".git").exists() or (parent / ".git").is_symlink() for parent in (cwd, *cwd.parents)):
            raise RuntimeFailure("The private launcher directory must be outside every Git working tree.")
        with _private_file(stdout_path) as events, _private_file(stderr_path) as diagnostics:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=_environment(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            if on_started is not None:
                on_started(process.pid)
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeFailure("Could not establish Claude's control streams.")
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                outgoing = bytearray()
                buffered = bytearray()
                total = 0
                stderr_total = 0
                startup_stderr = 0
                phase = "initialize"
                submitted = False
                initialize_id, settings_id = uuid.uuid4().hex, uuid.uuid4().hex

                def enqueue(message: dict[str, Any]) -> None:
                    if process is None or process.stdin is None or process.stdin.closed:
                        raise RuntimeFailure("Claude closed its control input before review completion.")
                    outgoing.extend(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                    try:
                        selector.get_key(process.stdin)
                    except KeyError:
                        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")

                def receive(line: bytes) -> None:
                    nonlocal phase, submitted
                    if not line.strip():
                        return
                    if len(line) > MAX_RECORD_BYTES:
                        raise RuntimeFailure("Claude exceeded the maximum stream-record size.")
                    try:
                        message = json.loads(line, object_pairs_hook=_unique_object)
                    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                        raise RuntimeFailure("Claude emitted an invalid JSON stream record.") from exc
                    if not isinstance(message, dict):
                        raise RuntimeFailure("Claude emitted an invalid stream envelope.")
                    kind = message.get("type")
                    if kind == "control_response":
                        if submitted:
                            raise RuntimeFailure("Claude emitted an unexpected control response; the review is incomplete.")
                        response = _mapping(message.get("response"), "control response")
                        request_id = response.get("request_id")
                        expected_id = initialize_id if phase == "initialize" else settings_id if phase == "settings" else None
                        if expected_id is None or request_id != expected_id or response.get("subtype") != "success":
                            raise RuntimeFailure("Claude does not support the required policy-inspection protocol; review was not submitted.")
                        body = _mapping(response.get("response"), "control response")
                        if phase == "initialize":
                            if body.get("current_permission_mode") != "dontAsk":
                                raise RuntimeFailure("Claude did not initialize in dontAsk mode; review was not submitted.")
                            phase = "settings"
                            enqueue({"type": "control_request", "request_id": settings_id, "request": {"subtype": "get_settings"}})
                        else:
                            result.policy_summary = verify_effective_policy(body, policy)
                            phase = "review"
                            events.write(json.dumps({"type": "runner_policy", **result.policy_summary}).encode("utf-8") + b"\n")
                            if startup_stderr:
                                diagnostics.write(b"Startup diagnostics omitted before policy verification.\n")
                            enqueue({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None, "session_id": ""})
                            submitted = True
                        return
                    if kind == "control_request":
                        if not submitted:
                            raise RuntimeFailure("Claude requested a callback before policy verification; review was not submitted.")
                        request_id = message.get("request_id")
                        request = message.get("request")
                        if not isinstance(request_id, str) or not isinstance(request, dict):
                            raise RuntimeFailure("Claude emitted an invalid permission request.")
                        if request.get("subtype") == "can_use_tool":
                            response = {
                                "subtype": "success",
                                "request_id": request_id,
                                "response": {"behavior": "deny", "message": "The review runner does not grant additional permissions."},
                            }
                        else:
                            response = {"subtype": "error", "request_id": request_id, "error": "This review does not support interactive callbacks."}
                        enqueue({"type": "control_response", "response": response})
                        events.write(b'{"type":"runner_callback","decision":"denied"}\n')
                        return
                    if not submitted:
                        if kind in {"assistant", "user", "result"}:
                            raise RuntimeFailure("Claude began a conversation before policy verification; review was not submitted.")
                        return
                    if kind == "result":
                        if result.payload:
                            raise RuntimeFailure("Claude emitted more than one final result.")
                        if process.stdin is None:
                            raise RuntimeFailure("Claude's control input is unavailable.")
                        result.payload = message
                        outgoing.clear()
                        try:
                            selector.unregister(process.stdin)
                        except KeyError:
                            pass
                        process.stdin.close()
                    events.write(line + b"\n")
                    events.flush()

                enqueue({"type": "control_request", "request_id": initialize_id, "request": {"subtype": "initialize"}})
                while selector.get_map():
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        result.timed_out = True
                        raise RuntimeFailure("Claude exceeded the review timeout.")
                    ready = selector.select(min(remaining, 0.25))
                    for key, _ in ready:
                        stream = key.fileobj
                        if key.data == "stdin":
                            try:
                                written = os.write(key.fd, outgoing)
                            except (BlockingIOError, InterruptedError):
                                continue
                            except BrokenPipeError as exc:
                                raise RuntimeFailure("Claude closed its input before the review completed.") from exc
                            del outgoing[:written]
                            if not outgoing:
                                selector.unregister(stream)
                            continue
                        try:
                            chunk = os.read(key.fd, 65536)
                        except (BlockingIOError, InterruptedError):
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            if key.data == "stdout" and buffered:
                                receive(bytes(buffered))
                                buffered.clear()
                            continue
                        if key.data == "stderr":
                            stderr_total += len(chunk)
                            if stderr_total > MAX_STDERR_BYTES:
                                raise RuntimeFailure("Claude exceeded the maximum diagnostic-output size.")
                            if submitted:
                                diagnostics.write(chunk)
                            else:
                                startup_stderr += len(chunk)
                            continue
                        total += len(chunk)
                        if total > MAX_STREAM_BYTES:
                            raise RuntimeFailure("Claude exceeded the maximum review-stream size.")
                        buffered.extend(chunk)
                        while b"\n" in buffered:
                            line, _, tail = buffered.partition(b"\n")
                            buffered = bytearray(tail)
                            receive(bytes(line))
                        if len(buffered) > MAX_RECORD_BYTES:
                            raise RuntimeFailure("Claude exceeded the maximum stream-record size.")
                if not result.payload:
                    raise RuntimeFailure("Claude exited without a completed review result.")
                try:
                    process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired as exc:
                    result.timed_out = True
                    raise RuntimeFailure("Claude exceeded the review timeout while exiting.") from exc
                if process.returncode != 0:
                    raise RuntimeFailure("Claude exited unsuccessfully; the review is incomplete.")
    except KeyboardInterrupt:
        result.interrupted = True
        result.error = "Claude review was interrupted."
    except RuntimeFailure as exc:
        result.error = str(exc)
    except (OSError, ValueError, TypeError):
        result.error = "Could not prepare or operate the private Claude review process and artifacts."
    finally:
        if process is not None:
            try:
                _reap(process)
            except (OSError, subprocess.TimeoutExpired):
                result.error = result.error or "Could not confirm termination of the Claude worker group."
            result.returncode = process.returncode if process.returncode is not None else 1
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return result
