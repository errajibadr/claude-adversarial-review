PYTHON ?= python3
export REVIEW_PROMPT REVIEW_MODEL REVIEW_TIMEOUT

.PHONY: review check

review: ## Run the inspected review packet with Claude
	$(PYTHON) plugins/claude-adversarial-review/scripts/review.py --prompt-file "$$REVIEW_PROMPT" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" < /dev/null

check: ## Validate the packet and command without calling Claude
	$(PYTHON) plugins/claude-adversarial-review/scripts/review.py --prompt-file "$$REVIEW_PROMPT" --model "$${REVIEW_MODEL:-opus}" --timeout "$${REVIEW_TIMEOUT:-300}" --dry-run < /dev/null
