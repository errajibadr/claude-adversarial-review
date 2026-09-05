# Contributor instructions

This repository distributes a dependency-free Codex plugin and an optional
Claude Code command. Keep the installed plugin self-contained: resolve resources
relative to its skill, never from the repository being reviewed.

- Review workers must not dispatch other reviewers, edit code, or install tools.
- Preserve tool/MCP/customization restrictions and truthful failure reporting.
- Never commit prompts from private reviews, credentials, client context, review
  artifacts, or machine-local absolute paths. Keep public paths repo-relative.
- Verify changes with proportional offline smoke checks outside the repository;
  do not add committed test files for local scripts by default.
- Use conventional commits and check the exact staged file list before pushing.
- Changes to subprocess behavior require a scoped independent review. Review
  workers are terminal workers; this rule does not apply recursively to them.
- Do not change release versions or add dependencies without explicit approval.
