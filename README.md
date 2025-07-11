Accounting helpers for Stripe
===================================

Millions of businesses of all sizes—from startups to large enterprises—use
Stripe's software and APIs to accept payments, send payouts, and manage their
businesses online.

However, Stripe lacks some functionalities or has some workflow issues,
including, but not restricted to:
- providing credit note options for voided and uncollectible invoices
- linking payout items to invoices and credit notes
- emitting automatically credit notes when a dispute is lost
- providing good statistics about OSS (One Stop Shop)

This repository aims to provide toolings to fix these issues and help
businesses to be compliant with their country accounting standards.

Feel free to open an issue with your business requirements.

The repository is experimental and may contain some LeakIX specific business
requirements.

## Setup

Install python dependencies:
```bash
make install
```

Or directly with poetry:
```bash
poetry install
```

### Environment variables

Some configuration is required. Create a file `.env` based on `template.env`.

## Development

### Available Make targets

Run `make help` to see all available targets:

```bash
make help
```

### Development workflow

```bash
make dev-check  # Run formatting, linting, and tests
make format     # Format code with black
make lint       # Run linting checks
make test       # Run tests
make clean      # Clean generated files and cache
```

## CLI Usage

### Download all invoices between two dates

Using Make:
```bash
make download-invoices FROM_DATE=2023-03-01 TO_DATE=2023-03-31
```

Or directly with poetry:
```bash
poetry run python stripe_accounting/accounting.py download-invoices \
  --from-datetime 2023-03-01 \
  --until-datetime 2023-03-31
```

Files will be available in `DOWNLOAD_DIRECTORY`, see `template.env`

### Emit credit notes

Emit credit notes between two dates. It emits credits notes for:
- voided invoices
- uncollectible invoices
- Stripe emitted credit notes (to keep a continuous numbering)
- opened invoices if [include_open] is set to non-zero value
- include refunded invoices. There should be an overlap with the stripe
  emitted CN but we might have forgotten to emit a credit note when refunding
- include lost disputes

Using Make:
```bash
make emit-credit-notes FROM_DATE=2023-03-01 TO_DATE=2023-03-31 \
  INDEX=3 CURRENCY=eur ISSUE_DATE=2023-03-31 INCLUDE_OPEN=1
```

Or directly with poetry:
```bash
poetry run python stripe_accounting/accounting.py emit-credit-notes \
  --from-datetime 2023-03-01 \
  --until-datetime 2023-03-31 \
  --first-index-cn 3 \
  --currency-iso-code eur \
  --include-open 1 \
  --issued-date-credit-note 2023-03-31 \
  --skipping-invoices ""
```

### Create VAT detailed report

Using Make:
```bash
make vat-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 FORMAT=xlsx
```

Or directly with poetry:
```bash
poetry run python stripe_accounting/accounting.py make_detailled_vat_report \
  --from-datetime 2023-07-01 \
  --until-datetime 2023-07-31 \
  --output-extension xlsx
```

### Create detailed payout report

Links every invoice with a payout, include fees, country, client, etc.

Using Make:
```bash
make payout-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 FORMAT=xlsx
```

Or directly with poetry:
```bash
poetry run python stripe_accounting/accounting.py export_payout \
  --from-datetime 2023-07-01 \
  --until-datetime 2023-07-31 \
  --output-extension xlsx
```

## Examples

The Makefile includes example targets for common use cases:

```bash
make example-download      # Download invoices for March 2023
make example-credit-notes  # Generate credit notes for March 2023
make example-vat          # Generate VAT report for July 2023
make example-payout       # Generate payout report for July 2023
```
