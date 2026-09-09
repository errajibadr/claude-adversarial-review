PYTHON ?= python3
export REVIEW_REPO REVIEW_PROMPT REVIEW_FOCUS REVIEW_MODEL REVIEW_TIMEOUT REVIEW_CONTEXT_MODE

.PHONY: review check

review: ## Review a repository or explicitly supplied packet with Claude
	@if [ -n "$$REVIEW_PROMPT" ]; then \
	  $(PYTHON) plugins/claude-adversarial-review/scripts/review.py --prompt-file "$$REVIEW_PROMPT" --focus "$${REVIEW_FOCUS:-}" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" < /dev/null; \
	else \
	  $(PYTHON) plugins/claude-adversarial-review/scripts/review.py --repo "$${REVIEW_REPO:-.}" --context-mode "$${REVIEW_CONTEXT_MODE:-live}" --focus "$${REVIEW_FOCUS:-}" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" < /dev/null; \
	fi

check: ## Inspect scope or packet without calling Claude
	@if [ -n "$$REVIEW_PROMPT" ]; then \
	  $(PYTHON) plugins/claude-adversarial-review/scripts/review.py --prompt-file "$$REVIEW_PROMPT" --focus "$${REVIEW_FOCUS:-}" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" --dry-run < /dev/null; \
	else \
	  $(PYTHON) plugins/claude-adversarial-review/scripts/review.py --repo "$${REVIEW_REPO:-.}" --context-mode "$${REVIEW_CONTEXT_MODE:-live}" --focus "$${REVIEW_FOCUS:-}" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" --dry-run < /dev/null; \
	fi
