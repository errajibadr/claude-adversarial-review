# Claude Adversarial Review runner

Select a Git review scope and let Claude inspect it independently. The default
live mode reads the actual repository with read tools and restricted read-only
Git commands. Snapshot and tool-free packet modes are explicit alternatives.
The [skill](skills/claude-adversarial-review/SKILL.md) defines the host workflow;
the [repository README](https://github.com/errajibadr/claude-adversarial-review)
explains Codex installation and direct use from other coding assistants.

**Before installing or authorizing a host launch:** the Python runner, its Git
evidence collection, and Claude's main process execute outside Codex's sandbox
in the approved host workflow. Claude sandboxes its Bash commands by default;
an originally full-access task may explicitly select a configuration that disables
that sandbox, removing OS filesystem/network enforcement. Read-only command and
tool rules remain; Read, Glob, and Grep use application permission checks.
Installing the plugin does not grant host access. Use the host's native authorization flow and reuse
authorization already given; the plugin neither changes global permission rules
nor escapes an enclosing sandbox itself.

## Select scope and context mode

Resolve `scripts/review.py` from the installed plugin, not the project being
reviewed. From a source checkout:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --scope auto --focus 'Security and performance' \
  --dry-run < /dev/null
```

Inspect the selected paths, omissions, and candidate evidence before removing
`--dry-run`. A dry run calls no model and writes no artifacts. It prints scope
metadata rather than file bodies. It does not redact or certify source contents,
verify a live Claude session, or prove that a host can enforce the runtime policy.

| Option | Meaning |
| --- | --- |
| `--repo` | Target Git repository; defaults to the current directory. |
| `--context-mode live` | Default repository mode: inspect the actual repository using read tools and restricted Git. |
| `--context-mode snapshot` | Copy bounded evidence; Claude uses Read, Glob, and Grep without Git or Bash. |
| `--scope auto` | Dirty working tree, otherwise a branch comparison. |
| `--scope working-tree` | Staged, unstaged, and untracked changes. |
| `--scope branch` | Compare the merge base with HEAD. |
| `--base REF` | Explicit branch base; overrides scope. |
| `--path PATH` | Repeat to restrict changed targets to task files/directories. |
| `--exclude GLOB` | Repeat to omit paths from selected evidence; not a file-access restriction in live mode. |
| `--focus TEXT` | Preserve review priorities, accepted constraints, and existing check results. |
| `--prompt-file FILE` | Tool-free packet review; incompatible with explicit context mode and repository scope options. |
| `--model MODEL` | Default `opus`; requested and actual models are recorded. |
| `--timeout SECONDS` | Positive finite deadline; default 300. |
| `--sandbox-settings FILE` | Explicit live Bash sandbox override; takes precedence over `CLAUDE_ADVERSARIAL_REVIEW_SANDBOX_SETTINGS`. Unavailable in snapshot or packet mode. |
| `--host-sandbox-mode MODE` | Original task permissions: `restricted`, `full-access`, or `unknown` (default). Only `full-access` permits an explicit live sandbox disablement. |
| `--output-dir DIR` | Existing parent for a new private run directory; default system temp. |
| `--dry-run` | Show planned scope and options without calling Claude. |

Default branch detection uses origin's HEAD then main/master/trunk candidates.
Branch comparisons capture the resolved HEAD and merge base rather than relying
on a branch name remaining unchanged. Working-tree reviews distinguish staged,
unstaged, and untracked changes, including staged and unstaged changes that
cancel each other. An empty target is insufficient context.

Live and snapshot modes reject nonempty configured `filter.*.clean` or
`filter.*.process` commands before collecting scope diffs. Git can execute these
filters during ordinary diff operations even when external diff and text
conversion are disabled. This check conservatively rejects unused global filter
definitions too. Before any scope diff, an index/configuration preflight
recursively checks reachable initialized submodules, without reading or copying
source bodies. Absent or uninitialized checkouts are skipped. Unsafe or
unverifiable nested paths/configuration, or a preflight limit, block collection.
The preflight accepts at most 16 MiB of parent and nested index metadata, checked
after each Git command finishes writing its output to a temporary file. This
limits accepted and in-memory metadata, not temporary disk usage or work while
Git runs; each command has a 30-second timeout. Exceeding the metadata limit
reports a specific preflight-budget error. Narrowing `--path` does not bypass
this repository-wide check.
Such configurations are unsupported for repository collection; an explicitly
prepared `--prompt-file` remains available. There is no automatic fallback to
another mode.

## Live inspection: default

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --context-mode live --base main \
  --path src --path tests --focus 'Correctness, security, and API compatibility' \
  --model opus --timeout 300 < /dev/null
```

The runner captures the review target and prepares an inventory without copying
the repository into a snapshot. Up to two changed files and 256 KiB of diff are
inlined. For larger reviews, Claude starts with the inventory and collects
relevant diffs and surrounding source through the permitted tools. Deferred
collection does not imply that the large diff has already been reviewed.
Live scope and inventory are supplied in the initial prompt and retained in the
host's metadata; live mode does not create a snapshot `inventory.json` to read.

Claude starts in a private directory outside Git, with the actual repository
added as a permitted working directory. File reads use absolute target paths;
Git commands follow the runner's exact templates, including `git -C`, fixed
options, and captured revisions. Use those supplied forms rather than changing
flag order or substituting commands such as `git show`. The policy explicitly
permits the required read-only forms; it does not grant blanket Bash or Git
access. Diff templates allow only path operands after their final `--`;
historical supporting source uses the supplied `cat-file blob` template.
The separate launcher keeps Claude's bookkeeping out of the reviewed checkout.
Source locations in findings remain
repository-relative. Committed branch evidence comes from the captured Git
revisions; live file contents can differ when the checkout has unrelated edits.

**Scope controls are not privacy isolation in live mode.** `--path`, `--exclude`,
ignore rules, and default filename filters limit the evidence selected for the
review. They do not prevent the granted file tools from reading other accessible
contents in the repository. Do not use exclusions to promise that private files
are inaccessible. Choose snapshot mode or a carefully inspected packet for that
requirement. No mode automatically detects or redacts secrets.
In live mode, Git ignore filtering applies to untracked candidates; tracked
changes remain selected even when ignore rules match, unless explicit exclusions
or default private-path filters apply.

Live working-tree reviews treat clean submodule pointer changes as reviewable
Git links. Nested tracked or untracked uncommitted changes produce explicit
insufficient context; the collector does not recursively review submodule source.
Available pointer diffs remain reviewable even when that nested-content gap
prevents approval. An uninitialized checkout's recorded pointer comes from the
index, not an observed nested HEAD; its provenance is recorded explicitly. If
neither current pointer is observed, the value is null with `pointer_source`
set to `not-observed`, and a scope note distinguishes it from index-derived
pointers. Historical pointer changes, such as a captured deletion, remain reviewable.
Branch reviews inspect the captured pointer changes, leaving unrelated nested
working-tree edits outside their scope.

Live mode checks the selected scope again after the review. A changed selected
scope makes the effective outcome `insufficient-context` with exit 2, even if
Claude returned `approve`. The original findings remain available. A stable
selected scope does not prove that every supporting file or external dependency
remained unchanged.
Freshness compares selected content and relevant file kind/permissions, not
timestamp or inode identity alone. A content-preserving touch or atomic rewrite
does not invalidate the review if those semantics remain unchanged. Failure to
verify within the bounded check is reported separately from detected changes.

## Snapshot inspection

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --context-mode snapshot --scope working-tree \
  --path src --exclude 'private/**' --focus 'Error recovery and performance' \
  --dry-run < /dev/null
```

Remove `--dry-run` after inspecting the planned evidence. Snapshot mode collects
selected patches, bounded supporting tracked text source, and selected untracked
files. Branch snapshots use committed HEAD source. When paths narrow the scope,
unrelated dirty supporting files use HEAD content; substitutions are listed in
the inventory. Ignore rules from the checkout remain a collection filter even
for branch snapshots.

Small diffs use the same two-file/256 KiB inline threshold. Larger reviews give
Claude an inventory and patch files to inspect with Read, Glob, and Grep. Snapshot
mode grants no Bash or Git access and does not expose the actual repository as
an additional working directory. Source paths under `source/` map back to
repository-relative paths. Snapshot mode reviews the prepared copy; it does not
recheck the repository for subsequent changes.
Selected gitlink changes are listed despite submodule-ignore settings, but
snapshot mode deliberately omits both their source and pointer diffs. These
explicit selected omissions make the effective review outcome
`insufficient-context`; snapshot mode does not support pointer-only review.

Omitted files and collection limits are recorded. Selected-change omissions stay
explicit; large sets of supporting omissions are summarized by reason. Source
limits are 512 KiB per file and 16 MiB total. Planning considers up to 4,096
candidate files, prioritizing selected targets; later rejection of a candidate
can reduce the final snapshot count. Diffs are capped at 512 KiB each and 8 MiB
total. No Git database or unsafe symlinks are copied. Git collection disables
external diff/text conversion, filesystem monitor execution, and remote fetching
of missing objects. Missing selected objects prevent approval.

Filename filters are not secret detection, and omitted path names may still
appear in the inventory. Use an inspected packet when names themselves are
sensitive. Working-tree collection requires POSIX no-follow file access, as
available on macOS and Linux; unsupported platforms report omitted source.

## Packet-only mode

For a design document or curated evidence, prepare a private UTF-8 packet with
the [prompt](prompts/adversarial-review.md) as guidance. Include intended behavior,
explicit scope, line-numbered source, contracts, and existing check results.
Inspect its contents; the runner does not sanitize it. Maximum size: 512 KiB.

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --prompt-file "$review_prompt" --model opus --timeout 300 < /dev/null
```

Packet mode grants no tools and cannot discover missing source. Do not combine
it with `--context-mode` or repository scope options. It still uses the structured
review contract. Its coverage is limited to the supplied evidence; it cannot
check a repository's freshness.

## Results and background execution

The host can run this CLI in the foreground or with its native background
facility. Retain the process/session handle for status, completion, and
cancellation. This plugin does not recreate OpenAI's app-server job registry.
Do not interpret a background launch or quiet output as completion.

The private artifact directory contains execution metadata, raw Claude output,
stderr diagnostics, original structured `review.json`, and rendered `review.md`.
Snapshot reviews also retain prepared source and patches; live reviews retain
scope metadata without source copies. Keep artifacts outside Git;
they can contain source, private paths, and model output.

Require both a successful process and valid structured output. The runner
validates verdict, finding fields, location ranges, confidence, and coverage
limits. `approve`, `needs-attention`, and `insufficient-context` are distinct.
Read `metadata.json` for the effective outcome as well as the original report;
selected omissions or a stale selected scope can invalidate an apparent approval
without rewriting Claude's original findings.
The metadata records `context.context_mode` for repository modes and the top-level
`effective_verdict`. Live runs also record `scope_stability` with `unchanged` and
`reason`, and retain scrubbed protocol events
in `events.jsonl` rather than storing complete effective settings.
The host-mode fields `declared_host_sandbox_mode` and `host_mode_verified: false`
record the caller's declaration, not verified host permissions. Report the
effective Bash sandbox state separately, including any managed-policy decision
to keep it enabled.

- Exit 0: structurally valid, completed review; read its verdict and findings.
- Exit 1: preparation, execution, policy enforcement, or result validation failed.
- Exit 2: insufficient context, including a stale selected scope in live mode.
- Exit 64: invalid command-line arguments; no review ran.
- Exit 130: interrupted; the run is incomplete.

Argument or preparation failures can occur before a run directory exists. Only
read artifacts after the runner prints an artifact path; a missing directory
after such a failure never indicates a completed review.

Stream and diagnostic size caps and unexpected protocol records cause a failed
review. They are not treated as successful truncation, even if model work has
already occurred or partial artifacts remain.
Streams held open after a final payload remain subject to the finite review
deadline; a timeout is still a failure, with any captured payload retained.
Cleanup avoids signalling a completed, reaped leader to prevent PID reuse from
targeting an unrelated process. It does not guarantee that every detached CLI
helper has exited.

Return or link the original report unchanged, with its effective outcome and
coverage limits. Keep the implementing agent's interpretation separate. Findings
are recommendations to validate, not commands. Review workers do not apply fixes
or invoke another reviewer.

## Runtime boundaries

Prerequisites: Python 3.12+, Git for repository modes, and authenticated Claude
Code. No Python dependencies are required. The live permission and sandbox
protocol was tested with Claude Code 2.1.261. The runner verifies the CLI's
reported effective configuration and provenance, rejecting missing or conflicting
settings before submitting the prompt. When the Bash sandbox is enabled, OS
enforcement comes from Claude's native sandbox and the CLI honoring
`failIfUnavailable`. Disabled sandbox settings provide no OS enforcement.
Configuration verification is not an enforcement probe or cryptographic proof
against a broken or malicious CLI. Use a trusted installation; neither the
protocol nor a version number alone provides that assurance.

Live mode grants Read, Glob, and Grep plus explicit narrow permissions for the
runner's canonical read-only Git commands through Bash, sandboxed by default.
`dontAsk` remains enabled; a different command spelling may be denied even if
it appears equivalent. Use the supplied templates instead of broadening access.
Claude's built-in permission logic may automatically approve additional read-only
utilities. The runner does not grant blanket Bash access, and the review prompt
instructs the worker to use only the supplied Git commands through Bash.
When enabled, that sandbox denies writes to the reviewed repository and resolved
Git metadata and blocks subprocess network access by default. Explicit live sandbox settings
can customize the latter. Write/edit tools are unavailable and mutation commands
are not authorized;
private runtime working/configuration paths may still be written for bookkeeping.
Tests, arbitrary commands, and automatic sandbox fallbacks are not authorized
by the review workflow. Snapshot mode grants only the three file tools;
packet mode grants none. Do not remove a control or switch modes automatically
to make a blocked review appear successful.

All modes retain safe/restricted controls, strict empty MCP configuration,
explicit MCP denial, `dontAsk`, no session persistence, and schema-constrained
JSON results. User/repository customizations are suppressed; authentication and
managed policy remain in effect. The runner verifies the reported live
configuration before relying on Git tool access. A configuration or sandbox
failure is a failed review, not approval.

The CLI runs locally; inference uses the configured remote provider. Review
controls do not provide secret sanitization or prove runtime behavior. Include
existing check results: the reviewer cannot run tests or a browser. Repository
instructions can be reviewed as source data without granting them authority to
alter the worker's review policy.

The host chooses the execution boundary. An approved host launch puts the runner,
Git collection, and Claude's main process outside Codex's sandbox. The original
task's declared mode determines whether Claude's Bash sandbox must remain enabled;
host-launch approval does not change that mode. Review permissions remain fixed.
Disclose this boundary before the first host launch and use the host's native authorization flow.
Existing authorization persists. Do not change global rules automatically or
repeat a known failing sandboxed authentication attempt before each review.

A saved login can be inaccessible inside the sandbox without the account being
logged out. Check credential visibility with `claude auth status --json` in the
same execution environment; this does not prove network or review availability.
On macOS, Claude's native login uses Keychain services. A local check with Codex
0.153.2 and Claude Code 2.1.267 still returned unauthenticated with sandboxed
network enabled: the OS denied `com.apple.securityd.xpc`. Protected-file writes
remained denied. Network access alone therefore did not resolve that case, and
no supported Keychain-service grant was established. Another filesystem path
grant is not an established fix. If host execution is not authorized or supported,
the review remains blocked.

The plugin cannot change an enclosing sandbox from inside it. Use authentication
available in the approved execution environment; do not extract or copy saved
credentials or disable TLS verification. Machine-specific permission rules and
account configuration do not belong in the distributed plugin.

## Customize the live Bash sandbox

Live mode starts from the bundled [settings/sandbox.json](settings/sandbox.json).
Declare the original task's permissions with `--host-sandbox-mode`: `restricted`
or the default `unknown` requires `sandbox.enabled: true`; `full-access` permits
a user-selected configuration such as `{"sandbox":{"enabled":false}}`.
Bundled defaults remain
enabled in every mode. Derive this declaration from the original current task
permissions and preserve it across dry runs and approved host launches. Missing
environment markers, successful authentication, and configuration files do not
establish full access. The runner cannot authenticate this declaration.

To customize it, set `CLAUDE_ADVERSARIAL_REVIEW_SANDBOX_SETTINGS` to an absolute
or `~/` path to your JSON file. The runner reads it automatically; Codex does not
need to add a flag to each invocation. For a shell that passes the variable to
the runner:

```bash
export CLAUDE_ADVERSARIAL_REVIEW_SANDBOX_SETTINGS="$HOME/.config/claude-adversarial-review/sandbox.json"
```

For Codex, configure the path in your user `~/.codex/config.toml` so it reaches
tool subprocesses even with `shell_environment_policy.inherit = "core"`:

```toml
[shell_environment_policy.set]
CLAUDE_ADVERSARIAL_REVIEW_SANDBOX_SETTINGS = "~/.config/claude-adversarial-review/sandbox.json"
```

Add the entry to the existing table if present; do not duplicate the table or
replace other settings. If you use environment include filters, they must also
permit this variable. Restart Codex after changing its launch configuration;
exporting a variable in another terminal does not update an already running app.
This value is a file path, not a credential. The plugin does not modify your
Codex configuration. See [Codex environment policy](https://learn.chatgpt.com/docs/config-file/config-reference).

Selection order: explicit `--sandbox-settings FILE`, then a nonempty environment
variable, then bundled defaults. Only the selected custom file is overlaid on
the defaults. A missing or invalid selected file blocks the review; there is no
silent fallback. Snapshot and packet modes ignore the environment variable;
explicitly passing the flag in those modes remains an error.

The file must contain only a `sandbox` root. Keep it outside the plugin cache so
reinstallation does not replace it. No repository configuration or conventional
user settings path is discovered automatically. The selected source is recorded
in `sandbox_settings_source` in dry-run output and review metadata.

Objects merge recursively; arrays replace the bundled array. Filesystem paths
and Unix socket paths must be absolute or start with `~/`; relative paths are
rejected and accepted paths are normalized. Write paths must be concrete paths
without wildcards. For example, allowing a Bash network destination requires
clearing the default deny-all list:

```json
{
  "sandbox": {
    "network": {
      "allowedDomains": ["github.com"],
      "deniedDomains": []
    }
  }
}
```

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --context-mode live \
  --host-sandbox-mode "$review_host_sandbox_mode" \
  --sandbox-settings "$review_sandbox_settings" --dry-run < /dev/null
```

Set `review_sandbox_settings` to your file and `review_host_sandbox_mode` from the
original task's permissions as described above. The dry run includes the resolved
policy in the planned Claude command. Inspect it, then use the same option for
the real review. Customizable fields include filesystem read/write paths and
network domains, Unix sockets, Mach services, local binding, and strict
allowlisting. These settings constrain Bash; they do not expand or replace the
separate grants for Read, Glob, and Grep. Bash network controls are separate from
Claude's authentication and model-service connections.

Apart from the conditional `sandbox.enabled` choice, required values stay fixed:
`failIfUnavailable: true`, `allowUnsandboxedCommands: false`, empty exclusions,
weaker modes off, Apple Events off, and `filesystem.disabled: false`. The runner
always adds the repository and resolved Git metadata to `denyWrite`. **When the
sandbox is disabled, `denyWrite` and `deniedDomains` do not enforce OS filesystem
or network restrictions.** Read-only Git/tool permissions, empty MCP configuration,
`dontAsk`, and `autoAllowBashIfSandboxed: false` remain fixed. This customization
does not authorize tests or arbitrary commands.

Keep normal user and repository sandbox settings for interactive Claude work.
This reviewer intentionally ignores those files under `--restricted` and passes
its resolved policy through `--settings`. Managed settings still apply; they
cannot be overridden by this file. Managed policy may keep the sandbox enabled
despite a permitted `false` request; the runner accepts and reports that stricter
result. Claude's reported effective configuration is checked before the review
prompt is submitted. An unsupported or conflicting policy blocks the review.
Custom sandbox overrides have not yet been tested
with a live model session.

Primary documentation: [Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[permissions](https://code.claude.com/docs/en/permissions),
[sandboxing](https://code.claude.com/docs/en/sandboxing), and
[structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs).
