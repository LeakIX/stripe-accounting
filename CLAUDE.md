# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python-based accounting toolkit for Stripe businesses, providing functionality to handle common accounting workflow issues like credit notes, payouts, and VAT reporting that aren't well-supported by Stripe's default interface.

## Development Commands

### Setup
```bash
make install
# or: poetry install
```

### Development Workflow
```bash
make help        # Show all available targets
make dev-check   # Run formatting, linting, and tests
make format      # Format code with black
make lint        # Run linting checks
make test        # Run tests
make clean       # Clean generated files and cache
```

### Running the CLI
Use Make targets for common operations:
```bash
make download-invoices FROM_DATE=2023-03-01 TO_DATE=2023-03-31
make emit-credit-notes FROM_DATE=2023-03-01 TO_DATE=2023-03-31 INDEX=3 CURRENCY=eur ISSUE_DATE=2023-03-31
make vat-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 FORMAT=xlsx
make payout-report FROM_DATE=2023-07-01 TO_DATE=2023-07-31 FORMAT=xlsx
```

Or access the CLI directly:
```bash
poetry run python stripe_accounting/accounting.py <command> [options]
```

## Key Commands

### Download invoices
```bash
poetry run python stripe_accounting/accounting.py download-invoices --from-datetime 2023-03-01 --until-datetime 2023-03-31
```

### Generate credit notes
```bash
poetry run python stripe_accounting/accounting.py emit-credit-notes --from-datetime 2023-03-01 --until-datetime 2023-03-31 --first-index-cn 3 --currency-iso-code eur --include-open 1 --issued-date-credit-note 2023-03-31
```

To skip specific invoices, use the `--skipping-invoices` parameter with comma-separated values. Supports ranges:
```bash
# Skip single invoices
--skipping-invoices "25001-0001,25001-0005"

# Skip ranges (inclusive)
--skipping-invoices "25001-0010:25001-0020"

# Mixed: single invoices and ranges
--skipping-invoices "25001-0001,25001-0010:25001-0020,25001-0030"
```

### Generate VAT reports
```bash
poetry run python stripe_accounting/accounting.py make_detailled_vat_report --from-datetime 2023-07-01 --until-datetime 2023-07-31 --output-extension xlsx
```

### Generate payout reports
```bash
poetry run python stripe_accounting/accounting.py export_payout --from-datetime 2023-07-01 --until-datetime 2023-07-31 --output-extension xlsx
```

## Architecture

### Core Components

- **`accounting.py`**: Main CLI interface and core business logic classes
- **`report.py`**: Abstract reporting framework with Mattermost and Stdin implementations
- **`customer.py`**: Customer-related utilities (currently empty)

### Key Classes

- **`StripeAPI`**: Main CLI class exposing all commands
- **`Invoice`**: Represents Stripe invoices with methods for retrieval, filtering, and processing
- **`MadeUpCreditNote`**: Generates custom credit notes with HTML/PDF output
- **`Payout`**: Handles payout data and VAT categorization
- **`Customer`**: Customer data with B2B detection and country-based logic
- **`Currency`**: Multi-currency support (EUR, USD)

### Data Flow

1. **Stripe API Integration**: Uses official Stripe Python library to fetch invoices, payouts, disputes, etc.
2. **Processing**: Filters and categorizes data based on business rules (OSS countries, B2B vs B2C)
3. **Output Generation**: Creates credit notes (HTML/PDF), CSV/Excel reports, and pretty-printed tables

## Configuration

Create `.env` file based on `template.env` with:
- Stripe API keys
- Company information for credit notes
- Output directories for downloads and generated files

## Dependencies

- **Core**: `stripe`, `python-decouple`, `fire`, `jinja2`
- **Data**: `pandas`, `openpyxl`, `prettytable`
- **External**: `wget`, `wkhtmltopdf` (for PDF generation)

## Important Notes

- Credit notes use custom numbering format: `S{YY}{currency_index}1-{index}`
- VAT handling includes OSS (One Stop Shop) compliance for EU countries
- Supports both B2B and B2C invoice processing
- Uses multiprocessing for concurrent downloads
- HTML templates in `templates/` directory for credit note generation