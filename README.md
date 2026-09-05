# Claude Adversarial Review

Independent Claude review for Codex and other coding assistants, with security,
performance, code correctness, frontend, accessibility, architecture, and other
review lenses. The repository provides a Codex plugin and a standalone Python
runner that other assistants can invoke directly.

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

To pick up a published update, refresh this marketplace and explicitly reinstall:

```bash
codex plugin marketplace upgrade claude-adversarial-review
codex plugin add claude-adversarial-review@claude-adversarial-review
```

Marketplace refresh alone does not update the installed plugin. If you also
have the older `personal` variant installed, remove that variant after verifying
the Git installation when you want to replace it.

Start a fresh Codex task after installation. Prerequisites: Python 3.12+, Git,
and an authenticated Claude Code CLI supporting `--safe-mode` and `--restricted`
(restricted requires 2.1.248+). There are no Python package dependencies.
Check `claude --version` and `claude auth status`.

## Ask for a review

> Use claude-adversarial-review to review my current changes for security,
> performance, and code correctness. Include relevant untracked files.

> Review this branch against main. Challenge the checkout design and check
> frontend behavior, accessibility, and failure recovery.

The calling assistant inspects the scope and exclusions, launches Claude, and returns its original
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
an explicit packet-only mode and a configurable timeout.

## Use the runner directly

Other coding assistants can follow the
[skill](plugins/claude-adversarial-review/skills/claude-adversarial-review/SKILL.md)
and invoke the runner directly. Resolve the runner from this package and pass
the repository to review explicitly. Review scheduling and reciprocal-review
policies belong in the calling assistant's or consumer project's instructions.

From a source checkout, set `review_repo` to the repository you want reviewed:

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

## Scope and limits

- Snapshots omit ignored files, symlinks, binary content, common private paths,
  and files exceeding collection limits. Omissions are recorded; an omitted
  selected change cannot silently receive approval.
- Omitted contents may still have their path names listed in the inventory.
  Use packet mode when those names are themselves sensitive.
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
