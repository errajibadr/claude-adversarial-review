# Claude Adversarial Review plugin

Run an independent, scoped Claude review from Codex using an inspected evidence
packet. See the [repository README](https://github.com/errajibadr/claude-adversarial-review) for installation and the
optional Claude Code integration.

## Prepare the evidence

Follow the [skill](skills/claude-adversarial-review/SKILL.md) and fill in the
[prompt template](prompts/adversarial-review.md). Include the intended behavior,
selected review lenses, scoped diff, relevant line-numbered source, contracts,
and check results. Include staged, unstaged, and relevant untracked changes.

Write the packet to a private UTF-8 file outside Git. Inspect it before sending:
the runner does not collect files or redact secrets. Exclude credentials,
private data, unrelated work, ignored artifacts, and machine-local paths. The
maximum packet size is 512 KiB. Split larger work into coherent units while
retaining the context needed to review shared interfaces.

Claude receives this packet with no tools. It cannot inspect additional files,
run tests, or operate a browser. Include existing browser-check evidence for
frontend work and state any verification gaps explicitly.

## Run the review

Prerequisites: Python 3.12+ and an installed, authenticated Claude Code CLI that
supports the required safety flags. No Python package dependencies are needed.
Check `claude --version` and `claude auth status` if availability is uncertain.

From a source checkout, set `review_prompt` to the inspected packet's location:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --prompt-file "$review_prompt" --model opus --timeout 300 --dry-run < /dev/null

python3 plugins/claude-adversarial-review/scripts/review.py \
  --prompt-file "$review_prompt" --model opus --timeout 300 < /dev/null
```

For an installed plugin, resolve `scripts/review.py` from the installed plugin
directory. The skill resolves it relative to its own location; do not substitute
a similarly named script from the project under review. The source checkout
also provides `make check` and `make review` shortcuts.

Useful options:

| Option | Purpose |
| --- | --- |
| `--prompt-file` | Path to the inspected evidence packet. |
| `--model` | Requested model; defaults to `opus`. |
| `--timeout` | Maximum runtime in seconds; defaults to 300. |
| `--output-dir` | Existing parent directory for a unique run directory; defaults to system temp. |
| `--dry-run` | Validate input and inspect the planned invocation without calling a model. |

`sonnet`, `fable`, and provider-qualified model IDs are explicit alternatives when
supported by the configured provider. Aliases can resolve differently over
time or across providers. Inspect the returned model metadata instead of
assuming a specific model version. Account usage and billing apply.

## Read the result

The runner prints the private artifact directory. It contains:

- `review.md`: the review text when a successful result is available.
- `response.json`: the Claude CLI response.
- `stderr.log`: CLI diagnostics.
- `metadata.json`: execution status and requested/returned model information.

Require a successful response envelope as well as a successful process status.
An authentication error, timeout, malformed response, or CLI failure is a failed
review. A successful response may still report findings or insufficient context.
Neither should be presented as approval. Treat findings as recommendations to
validate, then let the implementing agent handle confirmed fixes.

## Execution boundaries

The runner uses Claude's print mode with tools disabled, an empty strict MCP
configuration, `dontAsk` permission mode, no session persistence, and safe mode.
This disables hooks, plugins, and automatic instruction loading while retaining
authentication and managed policies. It closes stdin after the finite packet,
uses subprocess arguments without a shell, and bounds execution time.

See the official [CLI reference](https://code.claude.com/docs/en/cli-reference)
and [model configuration documentation](https://code.claude.com/docs/en/model-config).
If an older CLI rejects the required flags, update it separately; do not remove
the safety flags to make a review run.

Local execution still sends the packet to the configured remote provider. This
workflow restricts reviewer capabilities; it is not a claim of complete OS
isolation or automatic data sanitization.

An enclosing Codex sandbox may make an existing Claude login unavailable. Use
the host's normal approval-controlled execution for the same restricted command
when permitted. Preserve all safety flags and never copy credentials or disable
TLS verification. If execution remains blocked, report the limitation and keep
the prepared packet available for an approved terminal invocation.
