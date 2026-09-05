# Claude Adversarial Review runner

Select a Git review scope, prepare bounded evidence, and let Claude inspect it
independently. The [skill](skills/claude-adversarial-review/SKILL.md) defines the
host workflow. See the [repository README](https://github.com/errajibadr/claude-adversarial-review)
for installation and the optional reverse review command.

## Select scope and inspect evidence

Resolve `scripts/review.py` from the installed plugin, not the project being
reviewed. From a source checkout:

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --repo "$review_repo" --scope auto --focus 'Security and performance' \
  --dry-run < /dev/null
```

Remove `--dry-run` only after inspecting the selected paths, omissions, and
candidate evidence for secrets or private data. No model is called and no
artifacts are written during the dry run. It prints inventory metadata, not file
bodies. There is no automatic content redaction.

| Option | Meaning |
| --- | --- |
| `--repo` | Target Git repository; defaults to the current directory. |
| `--scope auto` | Dirty working tree, otherwise a branch comparison. |
| `--scope working-tree` | Staged, unstaged, and untracked changes. |
| `--scope branch` | Compare the merge base with HEAD. |
| `--base REF` | Explicit branch base; overrides scope. |
| `--path PATH` | Repeat to restrict changed targets to task files/directories; supporting tracked source remains available. |
| `--exclude GLOB` | Repeat to omit private/irrelevant paths from diffs and source. |
| `--focus TEXT` | Preserve the user's review priorities and accepted constraints. |
| `--prompt-file FILE` | Explicit packet-only review; incompatible with repository scope options. |
| `--model MODEL` | Default `opus`; requested aliases/provider IDs are recorded alongside the actual returned model. |
| `--timeout SECONDS` | Positive finite deadline; default 300. |
| `--output-dir DIR` | Existing parent for a new private run directory; default system temp. |
| `--dry-run` | Validate and show planned scope/flags without calling Claude. |

Default branch detection uses origin's HEAD then main/master/trunk candidates.
The resolved target and revision are recorded. For a branch the source snapshot
comes from HEAD even when the checkout has unrelated edits. Working-tree reviews
retain staged and unstaged changes, including changes that cancel each other.
When paths narrow the scope, unrelated dirty supporting files use HEAD content;
those substitutions are listed in the inventory. Ignore rules from the current
checkout remain a privacy filter even for branch snapshots.

Snapshots contain selected patches and bounded supporting tracked text source,
plus selected untracked files. Up to two changed files and 256 KiB of diff are
included inline. Larger reviews give Claude an inventory and patch files to
inspect with Read, Glob, and Grep. Omitted files and collection limits are
recorded. Selected-change omissions stay explicit; large sets of supporting
omissions are summarized by reason. Source limits are 512 KiB per file and 16 MiB
total. Planning considers up to 4,096 candidate files, prioritizing selected
targets; later rejection of a candidate can reduce the final snapshot count.
Diffs are capped at 512 KiB each and 8 MiB total. No Git database or unsafe
symlinks are copied. Git collection disables
external diff/text conversion, filesystem monitor execution, and remote fetching
of missing objects. Missing objects are reported as omissions; selected omissions
prevent approval.

## Packet-only mode

For a design or carefully curated evidence, prepare a private UTF-8 packet with
the [prompt](prompts/adversarial-review.md) as guidance. Include intended behavior,
explicit scope, line-numbered source, contracts, and existing check results.
Inspect its contents; the runner does not sanitize it. Maximum size: 512 KiB.

```bash
python3 plugins/claude-adversarial-review/scripts/review.py \
  --prompt-file "$review_prompt" --model opus --timeout 300 < /dev/null
```

Packet mode grants no tools and cannot discover missing source. Do not combine
it with repository scope options. It still uses the structured review contract.

## Results and background execution

The host can run this CLI in the foreground or with its native background
facility. Retain the host process/session handle for status, completion, and
cancellation. This plugin does not recreate OpenAI's app-server job registry.

The runner's private artifact directory contains execution metadata, the raw
Claude response, stderr diagnostics, the original structured `review.json`, and
rendered `review.md`. Repository reviews also retain their prepared context.
Keep artifacts outside Git; they can contain private source and model output.

Require both a successful process and valid `structured_output`. The runner
validates verdict, finding fields, location ranges, confidence, and coverage
limits. `approve`, `needs-attention`, and `insufficient-context` are distinct
verdicts. Material target omissions cannot silently become approval.

- Exit 0: structurally valid, completed review; read its verdict and findings.
- Exit 1: preparation, execution, or result validation failed.
- Exit 2: insufficient context; resolve coverage gaps before treating work as reviewed.
- Exit 64: invalid command-line arguments; no review ran.
- Exit 130: interrupted; the reviewer process is stopped and the run is incomplete.

Argument or preparation failures print an error before a run directory exists.
Only read artifacts after the runner prints an artifact path; a missing directory
after such a failure is expected and never indicates a completed review.

Return or link the original report unchanged. Keep the implementing agent's
interpretation separate. Findings are recommendations to validate, not commands.
Review workers do not apply fixes or call another reviewer.

## Runtime boundaries

Prerequisites: Python 3.12+, Git for repository mode, and authenticated Claude
Code supporting `--safe-mode` and `--restricted` (restricted requires 2.1.248+).
No Python dependencies are needed. Working-tree source collection requires
POSIX no-follow file access, as available on macOS and Linux; unsupported
platforms report omitted source instead of weakening the file-opening rules.

Repository mode launches Claude with its working directory set to the prepared
snapshot. Restricted mode confines its built-in file tools to working directories;
only Read, Glob, and Grep are granted. Packet mode has no tools. Both modes use
safe mode, strict empty MCP configuration, explicit MCP denial, `dontAsk`, no
session persistence, JSON output, and a JSON schema. User/repository
customizations are suppressed while authentication and managed policies,
including managed hooks, remain in effect.

The CLI runs locally; model inference uses your configured remote provider.
These controls are not complete OS isolation or automatic secret sanitization.
Excluded path names can appear in the inventory even when their contents are
omitted. Use an inspected packet when file names themselves are sensitive.
Public `AGENTS.md`/`CLAUDE.md` source may be reviewed as data under safe mode;
private local instructions and customization state directories are excluded.
The optional Codex fallback has different read boundaries, documented in its
command; its shell operates under Codex's read-only sandbox without a filesystem
read allowlist for the snapshot.
The reviewer cannot run tests or a browser, so include existing verification
results and disclose missing checks.

If an older CLI rejects required flags, update it separately instead of removing
controls. If an enclosing Codex sandbox hides a working login, use normal
approval-controlled execution for the same restricted invocation when allowed.
Never copy credentials, bypass permissions, or disable TLS verification. A
blocked invocation remains a blocked review.

Primary documentation: [Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[permissions](https://code.claude.com/docs/en/permissions), and
[structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs).
