Acounting helpers for Stripe
===================================

Millions of businesses of all sizes—from startups to large enterprises—use
Stripe’s software and APIs to accept payments, send payouts, and manage their
businesses online.

However, Stripe lacks some functionalities or has some workflow issues, including, but not restricted to:
- providing credit note options for voided and uncollectible invoices
- linking payout items to invoices and credit notes
- emitting automatically credit notes when a dispute is lost
- providing good statistics about OSS (One Stop Shop)

This repository aims to provide toolings to fix these issues and help businesses
to be compliant with their country accounting standards.

Feel free to open an issue with your business requirements.

The repository is experimental and may contain some LeakIX specific business requirements.

## Setup

Install python dependencies
```
poetry install
```

### Environment variables

Some configuration is required. Create a file `.env` based on `template.env`.

## CLI

### Download all invoices between two dates

```
poetry run python stripe_accounting/accounting.py download-invoices --from-datetime 2023-03-01 --until-datetime 2023-03-31
```

Files will be available in `DOWNLOAD_DIRECTORY`, see `template.env`

### Emit credit notes

Emit credit notes between two dates. It emits credits notes for:
- voided invoices
- uncollectible invoices
- Stripe emitted credit notes (to keep a continuous numbering)
- opened invoices if [include_open] is set to non-zero value.
- include refunded invoices. There should be an overlap with the stripe emitted CN but we
  might have forgotten to emit a credit note when refunding.
- include lost disputes

```
poetry run python \
  stripe_accounting/accounting.py \
  emit-credit-notes \
  --from-datetime 2023-03-01 \ # Start date
  --until-datetime 2023-03-31 \ # End date
  --first-index-cn 3 \ # The index to start for the credit note number
  --currency-iso-code eur \
  --include-open 1 \ # If set to non-zero value, include currenctly open invoices
  --issued-date-credit-note 2023-03-31 \ # Date to use for the credit note issuance
  --skipping-invoices "" # Invoice ids to skip
```
