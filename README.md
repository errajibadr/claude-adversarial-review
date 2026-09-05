# Claude Adversarial Review

Independent Claude review of work done in Codex, with security, performance,
code correctness, frontend, accessibility, architecture, and other review lenses.

The reviewer can inspect a prepared source snapshot with read tools. The runner
selects Git changes and validates structured findings before reporting a verdict.
It uses your installed, authenticated Claude Code CLI; inference runs through
your configured remote Anthropic or provider service.

## Install in Codex

Add this Git URL in the Codex app's plugin marketplace dialog:

```text
https://github.com/errajibadr/claude-adversarial-review
```

Then install **Claude Adversarial Review**. Or use the Codex CLI:

```bash
codex plugin marketplace add https://github.com/errajibadr/claude-adversarial-review
codex plugin add claude-adversarial-review@claude-adversarial-review
```

Start a fresh Codex task after installation. Prerequisites: Python 3.12+, Git,
and an authenticated Claude Code CLI supporting `--safe-mode` and `--restricted`
(restricted requires 2.1.248+). There are no Python package dependencies.
Check `claude --version` and `claude auth status`.

## Ask for a review

> Use claude-adversarial-review to review my current changes for security,
> performance, and code correctness. Include relevant untracked files.

> Review this branch against main. Challenge the checkout design and check
> frontend behavior, accessibility, and failure recovery.

Codex inspects the scope and exclusions, launches Claude, and returns its original
report with coverage limits. `opus` is the default alias; another alias or full
provider model ID can be explicitly requested. Results record the actual model.
Your configured account's usage and billing apply.

## Following OpenAI's review pattern

This project follows the review-quality practices in OpenAI's
[Codex adversarial-review plugin](https://github.com/openai/codex-plugin-cc/tree/main/plugins/codex):

| Practice | This plugin |
| --- | --- |
| Concrete review scope | Auto selects dirty working-tree changes; otherwise branch comparison. Explicit base and task paths are supported. |
| Adaptive evidence | Small diffs inline; larger reviews use a source inventory and independently readable diff files. |
| Independent inspection | Claude reads surrounding source in a prepared snapshot using Read, Glob, and Grep. |
| Structured findings | Validated verdict, summary, severity, location, confidence, recommendation, next steps, and coverage limits. |
| Review only | The worker returns findings without applying fixes or invoking another reviewer. |
| Tracked execution | The host tracks background jobs; the runner stores private artifacts and distinguishes failure from verdict. |

There are deliberate runtime differences: OpenAI uses Codex's app server with
read-only Git access in the checkout and its own job registry. This plugin uses
Claude's CLI with a bounded source snapshot and host-managed jobs. It retains
an explicit packet-only mode and a configurable timeout. The Claude outer
sandbox workaround below is a separate compatibility adaptation.

## Run from a source checkout

Set `review_repo` to the repository you want reviewed:

```bash
make check REVIEW_REPO="$review_repo"
make review REVIEW_REPO="$review_repo" REVIEW_MODEL=opus REVIEW_TIMEOUT=300
```

For exact scope selection:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --base main --path src --path tests \
  --exclude 'private/**' --focus 'Challenge retry correctness and performance' \
  --dry-run < /dev/null
```

Inspect the inventory and candidate evidence, then remove `--dry-run` to run.
For manually prepared design/evidence packets, `make check REVIEW_PROMPT=...`
and `make review REVIEW_PROMPT=...` retain tool-free packet mode.
See the [runner documentation](plugins/claude-adversarial-review/README.md).

## Review Claude Code's work with Codex

OpenAI's official plugin provides `/codex:adversarial-review`. The optional
[`/codex-review` command](integrations/claude-code/commands/codex-review.md)
also documents a direct Codex fallback for Claude's enclosing sandbox.
Installing this Codex plugin does not activate that Claude Code command.

To install the optional command, set `consumer_project` to your target project
and run from this repository:

```bash
mkdir -p "$consumer_project/.claude/commands"
cp -i integrations/claude-code/commands/codex-review.md \
  "$consumer_project/.claude/commands/codex-review.md"
```

For Claude's Bash sandbox, merge `codex` into `sandbox.excludedCommands` in the
target project's local settings and reload the sandbox. Start the fallback
invocation with literal `codex`; a Node or shell wrapper can prevent exclusion
matching. Keep Codex's own read-only sandbox. The command documents diagnostics
when an enclosing environment still blocks access.

## Scope and limits

- Snapshots omit ignored files, symlinks, binary content, common private paths,
  and files exceeding collection limits. Omissions are recorded; an omitted
  selected change cannot silently receive approval.
- Filename filters are not secret detection. Inspect the scope and evidence;
  exclude credentials, private client data, and unrelated work before sending.
- Source snapshots allow supporting tracked source beyond the changed paths.
  Unrelated dirty supporting files use HEAD content when paths narrow the scope.
  Use exclusions for sensitive supporting files. Packet-only mode sends only
  the explicitly supplied packet (maximum 512 KiB).
- Claude cannot edit code, execute shell commands, delegate, or use a browser
  through the granted tools. User/repository customizations and MCP are disabled;
  managed policy, including managed hooks, remains in effect.
- A completed request is not approval. Failed, malformed, timed-out, or
  insufficient-context reviews are reported separately. Validate findings before
  applying fixes; source inspection does not replace tests or browser checks.

The [skill](plugins/claude-adversarial-review/skills/claude-adversarial-review/SKILL.md)
defines the workflow. This is an independent project, not an official Anthropic
or OpenAI plugin.
