# Makefile for Stripe Accounting Tools
# ==================================

# Default target
.PHONY: help
help: ## Show this help message
	@echo "Stripe Accounting Tools - Available targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

# Development targets (alphabetical order)
.PHONY: clean
clean: ## Clean generated files and cache
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name ".pytest_cache" -delete
	find . -type d -name ".mypy_cache" -delete

.PHONY: dev-check
dev-check: format lint test ## Run all development checks

.PHONY: format
format: ## Format code with black
	poetry run black .

.PHONY: install
install: ## Install dependencies using poetry
	poetry install

.PHONY: lint
lint: ## Run linting checks
	poetry run black --check .
	poetry run mypy stripe_accounting/

.PHONY: test
test: ## Run tests
	poetry run pytest stripe_accounting/test_accounting.py -v

.PHONY: type-check
type-check: ## Run type checking with mypy
	poetry run mypy stripe_accounting/

.PHONY: update
update: ## Update dependencies to latest versions
	poetry update

# CLI wrapper targets (alphabetical order)
.PHONY: download-invoices
download-invoices: ## Download invoices (requires FROM_DATE and TO_DATE)
	@if [ -z "$(FROM_DATE)" ] || [ -z "$(TO_DATE)" ]; then \
		echo "Usage: make download-invoices FROM_DATE=2023-03-01 TO_DATE=2023-03-31"; \
		exit 1; \
	fi
	poetry run python stripe_accounting/accounting.py download-invoices \
		--from-datetime $(FROM_DATE) \
		--until-datetime $(TO_DATE)

.PHONY: emit-credit-notes
emit-credit-notes: ## Emit credit notes (requires FROM_DATE, TO_DATE, INDEX, CURRENCY, ISSUE_DATE)
	@if [ -z "$(FROM_DATE)" ] || [ -z "$(TO_DATE)" ] || [ -z "$(INDEX)" ] || \
	   [ -z "$(CURRENCY)" ] || [ -z "$(ISSUE_DATE)" ]; then \
		echo "Usage: make emit-credit-notes FROM_DATE=2023-03-01 TO_DATE=2023-03-31 \\"; \
		echo "       INDEX=3 CURRENCY=eur ISSUE_DATE=2023-03-31 [INCLUDE_OPEN=1]"; \
		exit 1; \
	fi
	poetry run python stripe_accounting/accounting.py emit-credit-notes \
		--from-datetime $(FROM_DATE) \
		--until-datetime $(TO_DATE) \
		--first-index-cn $(INDEX) \
		--currency-iso-code $(CURRENCY) \
		--include-open $(or $(INCLUDE_OPEN),0) \
		--issued-date-credit-note $(ISSUE_DATE) \
		--skipping-invoices "$(or $(SKIP_INVOICES),)"

.PHONY: payout-report
payout-report: ## Generate payout report (requires FROM_DATE, TO_DATE)
	@if [ -z "$(FROM_DATE)" ] || [ -z "$(TO_DATE)" ]; then \
		echo "Usage: make payout-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 [FORMAT=xlsx]"; \
		exit 1; \
	fi
	poetry run python stripe_accounting/accounting.py export_payout \
		--from-datetime $(FROM_DATE) \
		--until-datetime $(TO_DATE) \
		--output-extension $(or $(FORMAT),xlsx)

.PHONY: vat-report
vat-report: ## Generate detailed VAT report (requires FROM_DATE, TO_DATE)
	@if [ -z "$(FROM_DATE)" ] || [ -z "$(TO_DATE)" ]; then \
		echo "Usage: make vat-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 [FORMAT=xlsx]"; \
		exit 1; \
	fi
	poetry run python stripe_accounting/accounting.py make_detailled_vat_report \
		--from-datetime $(FROM_DATE) \
		--until-datetime $(TO_DATE) \
		--output-extension $(or $(FORMAT),xlsx)

# Example targets with common use cases (alphabetical order)
.PHONY: example-credit-notes
example-credit-notes: ## Example: Generate credit notes for March 2023
	$(MAKE) emit-credit-notes FROM_DATE=2023-03-01 TO_DATE=2023-03-31 \
		INDEX=3 CURRENCY=eur ISSUE_DATE=2023-03-31 INCLUDE_OPEN=1

.PHONY: example-download
example-download: ## Example: Download invoices for March 2023
	$(MAKE) download-invoices FROM_DATE=2023-03-01 TO_DATE=2023-03-31

.PHONY: example-payout
example-payout: ## Example: Generate payout report for July 2023
	$(MAKE) payout-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31

.PHONY: example-vat
example-vat: ## Example: Generate VAT report for July 2023
	$(MAKE) vat-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31