# Claude Adversarial Review

An independent Claude review of work done in Codex. Choose a focus—security,
performance, code correctness, frontend, accessibility, or architecture—and
receive evidence-based findings before deciding what to change.

The plugin uses your installed, authenticated Claude Code CLI. It sends an
inspected evidence packet with tools and customizations disabled. The reviewer
cannot explore your checkout or apply fixes. The CLI runs locally; inference
uses your configured remote Anthropic or provider service.

## Install in Codex

In the Codex app's plugin marketplace dialog, add this Git URL:

```text
https://github.com/errajibadr/claude-adversarial-review
```

Then install **Claude Adversarial Review**. Or use the Codex CLI:

```bash
codex plugin marketplace add https://github.com/errajibadr/claude-adversarial-review
codex plugin add claude-adversarial-review@claude-adversarial-review
```

Start a fresh Codex task after installation. You need Python 3.12+ and an
installed Claude Code CLI with working authentication; the runner has no Python
package dependencies. Check the CLI with `claude --version` and
`claude auth status`.

## Ask for a review

> Use claude-adversarial-review to review my current changes for security,
> performance, and code correctness. Include relevant untracked files and
> identify missing context.

Or narrow the request:

> Use claude-adversarial-review with Opus to review the checkout flow for
> frontend correctness and accessibility.

Codex prepares a scoped packet, inspects its contents, runs Claude, and reports
the findings and coverage limits. Supported lenses also include privacy,
reliability, compatibility, testing, and operations. Frontend reviews can assess
supplied source and browser-check evidence; the reviewer cannot run a browser.

The default model is `opus`. You can explicitly request `sonnet`, `fable`, or a
full model ID supported by your provider. Aliases can change; the result
records the actual returned model. Your configured account's usage and billing
apply.

## Run from a source checkout

Prepare a private UTF-8 packet using the
[prompt template](plugins/claude-adversarial-review/prompts/adversarial-review.md).
Set `review_prompt` to its location outside Git, then run from this repository:

```bash
make check REVIEW_PROMPT="$review_prompt"
make review REVIEW_PROMPT="$review_prompt" REVIEW_MODEL=opus REVIEW_TIMEOUT=300
```

`make check` validates the packet and prints the planned invocation without a
model call. `make review` writes the result and diagnostics into a private
artifact directory. See the [runner documentation](plugins/claude-adversarial-review/README.md)
for direct Python usage, output files, and failure handling.

## Review Claude Code's work with Codex

The optional [`/codex-review` command](integrations/claude-code/commands/codex-review.md)
provides the reverse workflow. Installing this Codex plugin does not activate
that Claude Code command.

To install it manually, set `consumer_project` to your target project directory
and run from this repository's root:

```bash
mkdir -p "$consumer_project/.claude/commands"
cp -i integrations/claude-code/commands/codex-review.md \
  "$consumer_project/.claude/commands/codex-review.md"
```

Inspect the file before using `/codex-review` in Claude Code. The command
documents its prerequisites and direct Codex invocation. For Claude's Bash
sandbox, merge `codex` into `sandbox.excludedCommands` in the target project's
local settings and reload the sandbox. Keep Codex's own read-only sandbox, and
start the invocation with literal `codex`: a wrapper can prevent the exclusion
from matching. See the command for diagnostics when an enclosing sandbox still
blocks access.

## Scope and limits

- Only explicitly included evidence is sent. Packets are capped at 512 KiB;
  split larger work into coherent reviews.
- The runner performs no automatic redaction. Inspect the packet and exclude
  credentials, private data, unrelated changes, and machine-local paths.
- Claude runs with built-in tools, MCP servers, hooks, plugins, and automatic
  instruction loading disabled. Reviews do not edit files or invoke other
  reviewers.
- A timeout, failed request, incomplete response, or insufficient context is
  not a passing review. Validate proposed findings before applying fixes.

The [skill](plugins/claude-adversarial-review/skills/claude-adversarial-review/SKILL.md)
defines the complete workflow. This is an independent project; it is not an
official Anthropic or OpenAI plugin.
