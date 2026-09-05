# Contributor instructions

This repository supplies independent adversarial reviews performed by Claude.
It distributes a dependency-free runner for coding assistants and Codex plugin
packaging. Keep resources self-contained: resolve them relative to the installed
skill or runner, never from the repository being reviewed. Host-specific
reciprocal review policies belong in host or consumer-project instructions.

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
