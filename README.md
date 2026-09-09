# Claude Adversarial Review

Independent Claude review for Codex and other coding assistants, with security,
performance, code correctness, frontend, accessibility, architecture, and other
review lenses. This repository provides a Codex plugin and a standalone Python
runner that other assistants can invoke directly.

By default, Claude inspects the actual repository with read tools and restricted
read-only Git commands. Small diffs are included in its prompt; larger reviews
start from a scope inventory and let Claude collect the evidence it needs.
The runner validates structured findings and reports incomplete reviews honestly.
It uses your installed, authenticated Claude Code CLI. The CLI runs locally;
model inference uses your configured Anthropic or provider service.

```text
Coding assistant selects scope and focus
                  |
                  v
Runner captures target revisions and inventory
                  |
                  v
Claude reads source + inspects read-only Git diffs
                  |
                  v
Runner validates findings + checks live scope is unchanged
                  |
                  v
Original report and effective outcome return to the assistant
```

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
the Git installation when you want to replace it. Start a fresh Codex task after
installation.

Prerequisites: Python 3.12+, Git, and authenticated Claude Code. The live protocol
was tested with Claude Code 2.1.261. The runner checks the CLI's reported effective
configuration and provenance before sending the prompt; Claude's native sandbox
and `failIfUnavailable` provide enforcement. This assumes a trusted, functioning
CLI, not a stable protocol across versions or proof against a broken or malicious
binary. Snapshot and packet modes also require the supported safe/restricted
controls. Check `claude --version` and `claude auth status`. No Python package
dependencies are required.

Repository modes reject configured Git clean/process filter commands before
collecting diffs, including unused global filter definitions: ordinary Git diff
can execute them. An index/configuration preflight also checks reachable
initialized submodules recursively, without reading or copying source bodies.
Unchecked or unsafe nested configuration blocks repository collection. Use an
explicitly prepared `--prompt-file` instead; the runner does not switch modes automatically.

## Ask for a review

> Use claude-adversarial-review to review my current changes for security,
> performance, and code correctness. Include relevant untracked files.

> Review this branch against main. Challenge the checkout design and check
> frontend behavior, accessibility, and failure recovery.

The calling assistant inspects the scope, starts Claude, and returns the original
report with coverage limits. `opus` is the default alias; another alias or full
provider model ID can be explicitly requested. Results record the actual model.
Your configured account's usage and billing apply.

## Following OpenAI's review pattern

This project follows the review-quality practices in OpenAI's
[Codex adversarial-review plugin](https://github.com/openai/codex-plugin-cc/tree/main/plugins/codex):

| Practice | This plugin |
| --- | --- |
| Concrete review scope | Auto selects dirty working-tree changes; otherwise branch comparison. Explicit base and task paths are supported. |
| Adaptive evidence | Up to two changed files and 256 KiB of diff inline; larger reviews start with an inventory and defer detailed collection. |
| Independent inspection | In default live mode, Claude reads the actual repository and inspects diffs with restricted read-only Git commands. |
| Structured findings | Validated verdict, summary, severity, location, confidence, recommendation, next steps, and coverage limits. |
| Review only | The worker returns findings without applying fixes or invoking another reviewer. |
| Tracked execution | The host tracks background jobs; the runner stores private artifacts and distinguishes failure from verdict. |

OpenAI uses Codex's app server and its job registry. This plugin uses Claude's
CLI and host-managed jobs. It also offers explicit snapshot and tool-free packet
modes. Live-mode results include a check that the selected scope has not
changed during the review.

## Use the runner directly

Other coding assistants can follow the
[skill](plugins/claude-adversarial-review/skills/claude-adversarial-review/SKILL.md)
and invoke the runner directly. Resolve the runner from this package and pass
the repository to review explicitly. Review scheduling and reciprocal-review
policies belong in the calling assistant's or consumer project's instructions.

From a source checkout, set `review_repo` to the repository to review:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --scope auto --focus 'Security and performance' \
  --dry-run < /dev/null
```

Inspect the scope and candidate evidence, then run with the same options and
without `--dry-run`. Live mode is the default; it can also be selected explicitly:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --context-mode live --base main \
  --path src --path tests --focus 'Challenge retries and public API compatibility' \
  --model opus --timeout 300 < /dev/null
```

To review a bounded copy instead of granting access to the actual repository:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --context-mode snapshot \
  --exclude 'private/**' --focus 'Review the selected implementation' < /dev/null
```

For a manually prepared design or evidence packet, use `--prompt-file` alone.
Packet mode grants no tools and cannot be combined with `--context-mode` or
repository scope options. See the [runner documentation](plugins/claude-adversarial-review/README.md)
for collection limits, result handling, and examples. The `make check` and
`make review` shortcuts remain available from a source checkout.

## Scope and limits

- **Live mode gives access to the actual repository.** `--path`, `--exclude`,
  ignore rules, and filename filters control selected review evidence; they
  are not a privacy barrier against file reads. Use snapshot or an inspected
  packet when the reviewer must not access other repository contents.
- Live scope retains tracked changes even when Git ignore rules match them;
  explicit exclusions and default private-path filters still apply. Live
  working-tree reviews can inspect clean submodule pointer changes, while nested
  uncommitted changes require separate review and produce insufficient context.
- Snapshot mode copies bounded text evidence and grants Read, Glob, and Grep
  without Git or Bash. It omits ignored files, symlinks, binary content, common
  private paths, and over-limit files. Selected gitlink changes omit both source
  and pointer evidence, so snapshot review of those changes is incomplete.
  Relevant omissions are recorded.
- Omitted contents may still have path names listed in the inventory. Filename
  filters do not detect secrets. Use a curated packet when those names or
  ordinary-looking source files contain sensitive material.
- Live Git runs through Claude's native Bash sandbox, denying writes to the
  reviewed repository and resolved Git metadata, with subprocess network access
  blocked. Claude uses the runner's exact command templates; permissions cover
  those read-only forms, not blanket Bash or Git access. Claude's built-in
  permission logic may also approve other read-only utilities; the review prompt
  still instructs the worker to use only the supplied Git forms. Write/edit tools are
  unavailable and mutation commands are not
  authorized. Private runtime working/configuration paths may still be written
  for bookkeeping. Safe/restricted controls, empty MCP configuration, and
  `dontAsk` remain enabled; managed policy remains in effect.
- A completed request is not approval. Failed, malformed, timed-out, stale, or
  insufficient-context reviews are distinguished. If the selected live scope
  changes during review, the effective outcome becomes `insufficient-context` (exit 2)
  while the original findings are preserved.
  Content-preserving touches or atomic saves do not invalidate live reviews
  when file kind, contents, and permissions remain the same.
- The reviewer cannot run tests or a browser. Validate findings before applying
  fixes and include existing verification results in the review context.

This is an independent project, not an official Anthropic or OpenAI plugin.
