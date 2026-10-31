.DEFAULT_GOAL := tests
.PHONY: tests unit integration test-integration-container

tests: unit integration

unit:
	uv run pytest tests/unit

# Boots real VMs if `container` is running, but never uses real tokens.
integration:
	uv run pytest tests/integration

test-integration-container:
	@echo "WARNING: these tests run real Claude Code sessions in VMs."
	@echo "They use the token stored by 'cave token' and consume real usage on your"
	@echo "account: claude-haiku-5-5, capped at \$$0.10 per Claude run."
	@printf "Type yes to acknowledge and continue: "; \
	read -r answer; \
	if [ "$$answer" != "yes" ]; then echo "Not acknowledged. Nothing was run."; exit 1; fi
	CAVE_TEST_SPEND_TOKENS=1 uv run pytest claude_api_tests
