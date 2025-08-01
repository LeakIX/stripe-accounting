import datetime
import itertools
import logging
import os
import pathlib
from decimal import Decimal
from typing import Optional, List, Dict
from prettytable import PrettyTable

import decouple
import fire
import pycountry
import stripe
import wget
import csv
import pandas as pd
import enum
from report import (
    Mattermost,
    SubscriptionCanceledReport,
    Stdin,
    AVAILABLE_REPORTING_PLATFORMS,
    SubscriptionCreatedReport,
)

from jinja2 import Environment, FileSystemLoader
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool

logging.basicConfig(encoding="utf-8", level=logging.INFO)

PWD = pathlib.Path(".")
DOWNLOAD_DIRECTORY = pathlib.Path(decouple.config("DOWNLOAD_DIRECTORY"))
TEMPLATE_DIRECTORY = PWD / "templates"
CN_HTML_OUTPUT_DIRECTORY = pathlib.Path(decouple.config("CN_HTML_OUTPUT_DIRECTORY"))
CN_PDF_OUTPUT_DIRECTORY = pathlib.Path(decouple.config("CN_PDF_OUTPUT_DIRECTORY"))

# From https://www.destatis.de/Europa/EN/Country/Country-Codes.html
INTRACOM_COUNTRY_CODES = [
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LV",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "IS",
    "LI",
    "NO",
    "CH",
]


def get_reporting_platform(reporting_platform_name: str):
    ReportingPlatform = AVAILABLE_REPORTING_PLATFORMS.get(reporting_platform_name)
    if ReportingPlatform is None:
        available_middlewares = ", ".join(AVAILABLE_REPORTING_PLATFORMS.keys())
        raise Exception(
            f"{reporting_platform_name} is not a valid reporting_platform. Available reporting platforms are f{available_middlewares}"
        )
    reporting_platform_config = {
        k: decouple.config(k) for k in ReportingPlatform.CONFIGURATION_KEYS
    }
    return ReportingPlatform(**reporting_platform_config)


def create_directories():
    directories = [
        DOWNLOAD_DIRECTORY,
        CN_HTML_OUTPUT_DIRECTORY,
        CN_PDF_OUTPUT_DIRECTORY,
    ]
    for d in directories:
        if not os.path.exists(d):
            logging.info(f"Directory {d} does not exist. Creating it...")
            os.mkdir(d)
            logging.info("Created")


JINJA2_ENV = Environment(loader=FileSystemLoader(searchpath=str(TEMPLATE_DIRECTORY)))

STRIPE_SECRET_KEY = decouple.config("STRIPE_SECRET_KEY")
stripe.api_key = STRIPE_SECRET_KEY


class Currency:
    def __init__(self, monetary_symbol: str, iso_code: str, internal_index: int):
        self.monetary_symbol = monetary_symbol
        self.iso_code = iso_code
        self.internal_index = internal_index

    def __eq__(self, other):
        return other.monetary_symbol == self.monetary_symbol


class Price:
    def __init__(self, q: Decimal, currency: Currency):
        self.q = q
        self.currency = currency

    # FIXME: improve when quantity >= 1000. Should be a better formatting. Existing lib?
    def __str__(self):
        if self.q >= 1000 and self.q < 1000000:
            f = str(self.q // 1000)
            s = self.q % 1000
            return "%s,%.2f" % (f, s)
        else:
            return "%.2f" % self.q

    def __eq__(self, other):
        return self.q == other.q and self.currency == other.currency

    def __add__(self, other):
        if self.currency.iso_code != other.currency.iso_code:
            raise Exception("Not the same currency")
        return Price(self.q + other.q, self.currency)

    @classmethod
    def sum(cls, l):
        if len(l) == 0:
            raise Exception("Empty list")
        elif len(l) == 1:
            return l[0]
        currency = l[0].currency
        s = cls(q=Decimal(l[0].q), currency=currency)
        tl = l[1:]
        for i in tl:
            s += i
        return s

    def abs(self):
        if self.q >= Decimal("0"):
            return self
        else:
            return Price(q=-self.q, currency=self.currency)


class Dispute:
    def __init__(self, raw: dict):
        self.raw = raw
        self._invoice = None

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def payment(self):
        return Payment(stripe.PaymentIntent.retrieve(self.raw["payment_intent"]))

    def is_lost(self):
        return self.raw["status"] == "lost"

    def is_warning_closed(self):
        return self.raw["status"] == "warning_closed"

    @property
    def invoice(self):
        if self._invoice is None:
            raw_charge = stripe.Charge.retrieve(self.raw["charge"])
            raw_invoice = raw_charge["invoice"]
            self._invoice = Invoice.retrieve_by_id(raw_invoice)
        return self._invoice

    @classmethod
    def retrieve(
        cls, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        has_more = True
        starting_after = None
        disputes = []
        while has_more:
            response = stripe.Dispute.list(limit=100, starting_after=starting_after)
            has_more = response["has_more"]
            raw_disputes = response["data"]
            starting_after = raw_disputes[-1]["id"]
            ds = [cls(raw=d) for d in raw_disputes]
            ds = [
                d for d in ds if from_datetime <= d.created_datetime <= until_datetime
            ]
            disputes.extend(ds)
        return disputes


class BalanceTransaction:
    def __init__(self, raw: dict):
        self.raw = raw


class VATReportItemCategory:
    BILLING = "Billing Fees"
    TAX_PRODUCT_FEES = "Tax Product Fees"
    STRIPE_PROCESSING_FEES_CARD = "Stripe Processing Fees (card)"
    STRIPE_PROCESSING_FEES_OTHER = "Stripe Processing Fees (other)"
    RADAR_FRAUD_FEES = "Radar Stripe Fees"
    REFUND_FOR_CHARGES = "Disputes"
    CHARGEBACK_WITHDRAWAL = "Dispute Fees"
    BANK_ACCOUNT = "Bank account"


class VATReportItem:
    def __init__(self, category: VATReportItemCategory, amount: Price, raw: dict):
        self.category = category
        self.amount = amount
        self.raw = raw


class Payout:
    def __init__(self, raw: dict):
        self.raw = raw
        self._items = None

    @property
    def items(self):
        if self._items is None:
            has_more = True
            payout_items = []
            starting_after = None
            while has_more:
                raw_response = stripe.BalanceTransaction.list(
                    limit=10, payout=self.payout_id, starting_after=starting_after
                )
                has_more = raw_response["has_more"]
                raw_items = raw_response["data"]
                payout_items.extend(
                    [PayoutItem(i, self) for i in raw_items if i["type"] != "payout"]
                )
                if has_more is True:
                    starting_after = raw_items[-1]["id"]
            self._items = payout_items
        return self._items

    @property
    def charges(self):
        return [i for i in self._items if i.is_charge()]

    @property
    def stripe_fees(self):
        return [i for i in self._items if i.is_stripe_fee()]

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def arrival_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["arrival_date"])

    @property
    def currency(self):
        return CURRENCIES.get(self.raw["currency"].upper())

    @property
    def amount(self):
        q = Decimal(self.raw["amount"]) / 100
        return Price(currency=self.currency, q=q)

    @property
    def payout_id(self):
        return self.raw["id"]

    @classmethod
    def retrieve(
        cls, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        has_more = True
        starting_after = None
        payouts = []
        while has_more:
            raw_response = stripe.Payout.list(limit=100, starting_after=starting_after)
            has_more = raw_response["has_more"]
            raw_payouts = raw_response["data"]
            payouts.extend([Payout(p) for p in raw_payouts])
            if has_more is True:
                starting_after = raw_payouts[-1]["id"]
        return [
            r for r in payouts if from_datetime <= r.arrival_datetime <= until_datetime
        ]

    def as_prettytable(self):
        items = self.items
        table = PrettyTable()
        table.field_names = [
            "description",
            "type",
            "Gross amount",
            "Net amount",
            "Fee amount",
            "Datetime",
            "Related invoice",
            "Client email",
            "Client country",
            "Related OSS accounting account",
        ]
        for i in items:
            if i.related_invoice is not None:
                table.add_row(
                    [
                        i.description,
                        i.item_type,
                        i.gross_amount,
                        i.net_amount,
                        i.fee_amount,
                        i.created_datetime,
                        i.related_invoice.number,
                        i.related_invoice.customer.email,
                        i.related_invoice.customer.address.country,
                        i.related_accounting_account,
                    ]
                )
            else:
                table.add_row(
                    [
                        i.description,
                        i.item_type,
                        i.gross_amount,
                        i.net_amount,
                        i.fee_amount,
                        i.created_datetime,
                        "",
                        "",
                        "",
                        "",
                    ]
                )
        return table


class PayoutItem:
    def __init__(self, raw: dict, payout: Payout):
        self.raw = raw
        self._related_invoice = None
        self.payout = payout

    @property
    def item_type(self):
        return self.raw["type"]

    # VAT category
    def is_billing_fees(self):
        return self.raw["description"].startswith("Billing")

    def is_tax_product_fees(self):
        return self.raw["description"].startswith("Automatic Taxes")

    def is_stripe_processing_fees_card(self):
        return self.raw["description"].startswith("Subscription") and self.is_charge()

    def is_stripe_processing_fees_other(self):
        return self.raw["description"].startswith("Subscription") and self.is_payment()

    def is_radar_fees(self):
        return self.raw["description"].startswith("Radar")

    def is_refund_fees(self):
        return self.raw["description"].startswith("REFUND FOR CHARGE")

    def is_refund_payment(self):
        return self.raw["description"].startswith("REFUND FOR PAYMENT")

    def is_chargeback_withdrawal_fees(self):
        return self.raw["description"].startswith("Chargeback withdrawal")

    def get_corresponding_vat_report_item(self):
        if self.is_billing_fees():
            return VATReportItem(
                VATReportItemCategory.BILLING, self.gross_amount.abs(), self.raw
            )

        elif self.is_tax_product_fees():
            return VATReportItem(
                VATReportItemCategory.TAX_PRODUCT_FEES,
                self.gross_amount.abs(),
                self.raw,
            )

        elif self.is_stripe_processing_fees_card():
            return VATReportItem(
                VATReportItemCategory.STRIPE_PROCESSING_FEES_CARD,
                self.fee_amount.abs(),
                self.raw,
            )

        elif self.is_stripe_processing_fees_other():
            return VATReportItem(
                VATReportItemCategory.STRIPE_PROCESSING_FEES_OTHER,
                self.fee_amount.abs(),
                self.raw,
            )

        elif self.is_radar_fees():
            return VATReportItem(
                VATReportItemCategory.RADAR_FRAUD_FEES,
                self.gross_amount.abs(),
                self.raw,
            )

        elif self.is_refund_fees():
            return VATReportItem(
                VATReportItemCategory.REFUND_FOR_CHARGES,
                self.fee_amount.abs(),
                self.raw,
            )

        elif self.is_refund_payment():
            return VATReportItem(
                VATReportItemCategory.BANK_ACCOUNT,
                self.fee_amount.abs(),
                self.raw,
            )

        elif self.is_chargeback_withdrawal_fees():
            return VATReportItem(
                VATReportItemCategory.CHARGEBACK_WITHDRAWAL,
                self.fee_amount.abs(),
                self.raw,
            )

        else:
            raise Exception(
                "Cannot assign a VAT report category. Description is %s, payout is %s (%s) and payout type is %s"
                % (
                    self.raw["description"],
                    str(self.payout.payout_id),
                    str(self.payout.arrival_datetime),
                    self.payout.raw["type"],
                )
            )

    @property
    def description(self):
        return self.raw["description"]

    def is_charge(self):
        return self.raw["type"] == "charge"

    def is_payment(self):
        return self.raw["type"] == "payment"

    def is_adjustment(self):
        return self.raw["type"] == "adjustment"

    def is_stripe_fee(self):
        return self.raw["type"] == "stripe_fee"

    def is_banking_account(self):
        return self.raw["type"] == "bank_account"

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def related_invoice(self):
        if self.is_charge() or self.is_payment():
            if self._related_invoice is None:
                charge_id = self.raw["source"]
                invoice = Invoice(
                    stripe.Charge.retrieve(charge_id, expand=["invoice"])["invoice"]
                )
                self._related_invoice = invoice
        return self._related_invoice

    @property
    def currency(self):
        return CURRENCIES.get(self.raw["currency"].upper())

    @property
    def gross_amount(self):
        q = Decimal(self.raw["amount"]) / 100
        return Price(currency=self.currency, q=q)

    @property
    def net_amount(self):
        q = Decimal(self.raw["net"]) / 100
        return Price(currency=self.currency, q=q)

    @property
    def fee_amount(self):
        gross_amount = Decimal(self.raw["amount"]) / 100
        net_amount = Decimal(self.raw["net"]) / 100
        q = gross_amount - net_amount
        return Price(currency=self.currency, q=q)

    @property
    def related_accounting_account(self):
        if not self.related_invoice.customer.is_b2b():
            if (
                self.related_invoice.customer_address.country_code
                not in INTRACOM_COUNTRY_CODES
            ):
                return "OSS EXTRACOM"
            else:
                return "OSS %s" % self.related_invoice.customer_address.country
        else:
            return "%s" % self.related_invoice.customer_name


class Payment:
    def __init__(self, raw: dict):
        self.raw = raw

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def invoice_id(self):
        return self.raw["invoice"]

    @property
    def invoice(self):
        raw_invoice = stripe.Invoice.retrieve(self.invoice_id)
        return Invoice(raw_invoice)


class Refund:
    def __init__(self, raw: dict):
        self.raw = raw

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def status(self):
        return self.raw["status"]

    def is_success(self):
        return self.stats == "succeeded"

    @property
    def charge_id(self):
        return self.raw["charge"]

    @property
    def payment(self):
        return Payment(stripe.PaymentIntent.retrieve(self.raw["payment_intent"]))

    @property
    def invoice(self):
        return self.payment.invoice

    @classmethod
    def retrieve(
        cls, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        refunds = []
        has_more = True
        starting_after = None
        while has_more:
            raw_response = stripe.Refund.list(limit=100, starting_after=starting_after)
            has_more = raw_response["has_more"]
            raw_refunds = raw_response["data"]
            refunds.extend([Refund(r) for r in raw_refunds])
            if has_more is True:
                starting_after = raw_refunds[-1]["id"]
        return [
            r for r in refunds if from_datetime <= r.created_datetime <= until_datetime
        ]


class CreditNote:
    def __init__(self, raw: dict):
        self.raw = raw

    @property
    def pdf_link(self):
        return self.raw["pdf"]

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def invoice_id(self):
        return self.raw["invoice"]

    @property
    def invoice(self):
        raw_invoice = stripe.Invoice.retrieve(self.invoice_id)
        return Invoice(raw_invoice)

    @property
    def number(self):
        return self.raw["number"]

    def get_name(self):
        d = self.created_datetime.strftime("%Y%m%d")
        return "%s - Credit note - %s" % (
            d,
            self.number,
        )

    def download(self):
        filename = DOWNLOAD_DIRECTORY / "{}.pdf".format(self.get_name())
        # TODO pdf link might be none when ???
        # finalized_at can be none when not finalized, we do not care about these
        # one.
        if self.pdf_link is not None and self.created_datetime is not None:
            wget.download(self.pdf_link, out=str(filename))
            logging.info(filename)

    @classmethod
    def retrieve(
        cls, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        """
        FIXME: add pagination
        """
        raw_credit_notes = stripe.CreditNote.list(limit=100)["data"]
        credit_notes = []
        for raw_c in raw_credit_notes:
            cn = cls(raw=raw_c)
            if (
                cn.created_datetime >= from_datetime
                and cn.created_datetime <= until_datetime
            ):
                credit_notes.append(cn)
        return credit_notes


# Follow ISO 4217
class TaxRate:
    def __init__(self, percentage, country_code):
        self.percentage = percentage
        self.country = pycountry.countries.get(alpha_2=country_code)

    @property
    def country_name(self):
        return self.country.name


class TaxRateCache:
    CACHE: Dict[str, TaxRate] = {}

    def get(self, tax_rate_id):
        if tax_rate_id in self.CACHE:
            return self.CACHE[tax_rate_id]
        else:
            tax_rate = stripe.TaxRate.retrieve(tax_rate_id)
            tax_rate = TaxRate(
                percentage=tax_rate["percentage"],
                country_code=tax_rate["country"],
            )
            self.CACHE[tax_rate_id] = tax_rate
            return tax_rate


TAX_RATE_CACHE = TaxRateCache()

# List of supported currencies, creating a mapping to use CURRENCIES.get(symbol)

CURRENCY_EUR = Currency(monetary_symbol="€", iso_code="EUR", internal_index=0)
CURRENCY_USD = Currency(monetary_symbol="$", iso_code="USD", internal_index=1)
CURRENCIES = {
    CURRENCY_EUR.iso_code: CURRENCY_EUR,
    CURRENCY_USD.iso_code: CURRENCY_USD,
}


class Product:
    def __init__(
        self,
        unit_price_excl_tax: Price,
        amount_excl_tax: Price,
        description: str,
        stripe_id: str,
        quantity: int,
        tax_rate: TaxRate,
    ):
        self.unit_price_excl_tax = unit_price_excl_tax
        self.amount_excl_tax = amount_excl_tax
        self.description = description
        self.stripe_id = stripe_id
        self.quantity = quantity
        self.tax_rate = tax_rate

    def __str__(self):
        return f"{self.description} - Qty = {self.quantity} - {self.stripe_id} - {self.unit_price_excl_tax} - {self.amount_excl_tax}"


class Address:
    def __init__(self, city, country_code, line1, line2, postal_code, state):
        self.city = city
        self.country_code = country_code
        self.line1 = line1
        self.line2 = line2
        self.postal_code = postal_code
        self.state = state

    @property
    def country(self):
        return pycountry.countries.get(alpha_2=self.country_code).name


class Customer:
    def __init__(self, name: str, email: str, address: Address, vat: Optional[str]):
        self.name = name
        self.email = email
        self.address = address
        self.vat = vat

    def is_b2b(self):
        return self.vat is not None

    def is_belgium_based(self):
        return self.address.country_code == "BE"

    @classmethod
    def retrieve_by_id(cls, customer_id):
        customer = stripe.Customer.retrieve(customer_id)
        address_info = customer["address"]
        address_info["country_code"] = address_info["country"]
        del address_info["country"]
        address = Address(**address_info)
        return Customer(
            address=address, name=customer["name"], email=customer["email"], vat=None
        )

    @classmethod
    def retrieve(cls):
        """
        TODO: add pagination
        """
        raw_customers = stripe.Customer.list(limit=1000)
        customers = []
        for c in raw_customers:
            address = Address(
                city=c["address"]["city"],
                country_code=c["address"]["country"] or "",
                line1=c["address"]["line1"] or "",
                line2=c["address"]["line2"],
                postal_code=c["address"]["postal_code"] or "",
                state=c["address"]["state"] or "",
            )
            tax_ids = c.get("customer_tax_ids", [])
            if len(tax_ids) == 0:
                vat = None
            elif len(tax_ids) == 1:
                tax_id = tax_ids[0]
                vat = tax_id["value"]
            else:
                raise Exception("Only supporting one tax ids for the moment")
            customer = Customer(
                name=c["name"], email=c["email"], address=address, vat=vat
            )
            customers.append(customer)
        return customers

    @classmethod
    def retrieve_by_country(cls, country_code: str):
        customers = cls.retrieve()
        customers = [c for c in customers if c.address.country_code == country_code]
        return customers


class INVOICE_STATUS:
    DRAFT = "draft"
    UNCOLLECTIBLE = "uncollectible"
    PAID = "paid"
    VOID = "void"
    OPEN = "open"


class EventType(enum.Enum):
    INVOICE_UPCOMING = "invoice.upcoming"
    INVOICE_FINALIZED = "invoice.finalized"
    INVOICE_PAID = "invoice.paid"
    INVOICE_UPDATED = "invoice.updated"
    INVOICE_PAYMENT_SUCCEEDED = "invoice.payment_succeeded"
    PAYMENT_INTENT_CREATED = "payment_intent.created"
    PAYMENT_INTENT_SUCCEEDED = "payment_intent.succeeded"
    CHARGE_SUCCEEDED = "charge.succeeded"
    CUSTOMER_SUBSCRIPTION_UPDATED = "customer.subscription.updated"
    INVOICE_CREATED = "invoice.created"
    CUSTOMER_SUBSCRIPTION_DELETED = "customer.subscription.deleted"
    CUSTOMER_SUBSCRIPTION_PAUSED = "customer.subscription.paused"
    CUSTOMER_SUBSCRIPTION_CREATED = "customer.subscription.created"

    @classmethod
    def from_str_opt(cls, s) -> Optional["EventType"]:
        return cls._value2member_map_.get(s)

    @classmethod
    def from_str_exn(cls, s) -> Optional["EventType"]:
        return cls._value2member_map_[s]


class Event:
    def __init__(self, raw):
        self.raw = raw

    @property
    def customer(self):
        customer = Customer.retrieve_by_id(self.raw["data"]["object"]["customer"])
        return customer

    @property
    def event_type_opt(self):
        return EventType.from_str_opt(self.raw["type"])

    @property
    def event_type_exn(self):
        return EventType.from_str_exn(self.raw["type"])

    @property
    def datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def canceled_at(self):
        return datetime.datetime.fromtimestamp(
            self.raw["data"]["object"]["canceled_at"]
        )

    @classmethod
    def retrieve(cls):
        events = stripe.Event.list()
        return [cls(e) for e in events["data"]]

    def is_customer_subscription(self):
        return self.event_type_exn in [EventType.CUSTOMER_SUBSCRIPTION_UPDATED]

    @classmethod
    def retrieve_new_subscription(cls):
        events = stripe.Event.list(
            type=EventType.CUSTOMER_SUBSCRIPTION_CREATED.value, limit=100
        )
        events = [cls(e) for e in events["data"]]
        return events

    @classmethod
    def retrieve_canceled_subscription(cls):
        events_deleted = stripe.Event.list(
            type=EventType.CUSTOMER_SUBSCRIPTION_DELETED.value, limit=100
        )
        events_deleted = [cls(e) for e in events_deleted["data"]]
        events_canceled = stripe.Event.list(
            type=EventType.CUSTOMER_SUBSCRIPTION_PAUSED.value, limit=100
        )
        events_canceled = [cls(e) for e in events_canceled["data"]]
        return events_canceled + events_deleted


class MadeUpCreditNote:
    COMPANY_NAME = decouple.config("COMPANY_NAME")
    COMPANY_ADDRESS_LINE_1 = decouple.config("COMPANY_ADDRESS_LINE_1")
    COMPANY_ADDRESS_LINE_2 = decouple.config("COMPANY_ADDRESS_LINE_2")
    COMPANY_ADDRESS_POSTAL_CODE = decouple.config("COMPANY_ADDRESS_POSTAL_CODE")
    COMPANY_ADDRESS_CITY = decouple.config("COMPANY_ADDRESS_CITY")
    COMPANY_ADDRESS_COUNTRY = decouple.config("COMPANY_ADDRESS_COUNTRY")
    COMPANY_EMAIL = decouple.config("COMPANY_EMAIL")
    COMPANY_VAT_NUMBER = decouple.config("COMPANY_VAT_NUMBER")

    def __init__(
        self,
        credit_note_number: str,
        invoice_number: str,
        issue_date_credit_note: datetime.datetime,
        customer: Customer,
        products: List[Product],
        subtotal: Price,
        amount: Price,
        tax_rate: Optional[TaxRate],
        subtotal_tax: Price,
        total_adjustment_amount: Price,
        adjustment_applied_to_invoice: Price,
    ):
        self.credit_note_number = credit_note_number
        self.invoice_number = invoice_number
        self.issue_date_credit_note = issue_date_credit_note
        self.customer = customer
        self.amount = amount
        self.products = products
        self.tax_rate = tax_rate
        self.subtotal = subtotal
        self.subtotal_tax = subtotal_tax
        self.total_adjustment_amount = total_adjustment_amount
        self.adjustment_applied_to_invoice = adjustment_applied_to_invoice

    def is_taxable(self):
        return self.tax_rate is not None

    def get_template_name(self):
        if self.is_taxable():
            return "credit_note_with_tax.html"
        else:
            return "credit_note_without_tax.html"

    def generate(self):
        template_name = self.get_template_name()
        with open(TEMPLATE_DIRECTORY / template_name, "r", encoding="utf-8") as f:
            template = f.read()
        template = JINJA2_ENV.get_template(template_name)
        if self.is_taxable():
            logging.info(
                f"Invoice {self.invoice_number} is taxable, therefore, we use {template_name}"
            )
            cn = template.render(
                template_directory=str(TEMPLATE_DIRECTORY.absolute()),
                credit_note_number=self.credit_note_number,
                invoice_number=self.invoice_number,
                issue_date_credit_note=self.issue_date_credit_note.strftime(
                    "%B %d, %Y"
                ),
                company_name=self.COMPANY_NAME,
                company_address_line_1=self.COMPANY_ADDRESS_LINE_1,
                company_address_line_2=self.COMPANY_ADDRESS_LINE_2,
                company_address_postal_code=self.COMPANY_ADDRESS_POSTAL_CODE,
                company_address_city=self.COMPANY_ADDRESS_CITY,
                company_address_country=self.COMPANY_ADDRESS_COUNTRY,
                company_email=self.COMPANY_EMAIL,
                company_vat_number=self.COMPANY_VAT_NUMBER,
                customer_name=self.customer.name,
                customer_address_line_1=self.customer.address.line1,
                customer_address_line_2=self.customer.address.line2,
                customer_address_postal_code=self.customer.address.postal_code,
                customer_address_city=self.customer.address.city,
                customer_address_country=self.customer.address.country,
                customer_email=self.customer.email,
                customer_vat=self.customer.vat or "",
                products=self.products,
                amount=self.amount,
                subtotal=self.subtotal,
                total_adjustment_amount=self.total_adjustment_amount,
                adjustment_applied_to_invoice=self.adjustment_applied_to_invoice,
                tax_rate=self.tax_rate.percentage,
                subtotal_tax=self.subtotal_tax,
            )
            return str(cn)
        else:
            cn = template.render(
                template_directory=str(TEMPLATE_DIRECTORY.absolute()),
                credit_note_number=self.credit_note_number,
                invoice_number=self.invoice_number,
                issue_date_credit_note=self.issue_date_credit_note.strftime(
                    "%B %d, %Y"
                ),
                company_name=self.COMPANY_NAME,
                company_address_line_1=self.COMPANY_ADDRESS_LINE_1,
                company_address_line_2=self.COMPANY_ADDRESS_LINE_2,
                company_address_postal_code=self.COMPANY_ADDRESS_POSTAL_CODE,
                company_address_city=self.COMPANY_ADDRESS_CITY,
                company_address_country=self.COMPANY_ADDRESS_COUNTRY,
                company_email=self.COMPANY_EMAIL,
                company_vat_number=self.COMPANY_VAT_NUMBER,
                customer_name=self.customer.name,
                customer_address_line_1=self.customer.address.line1,
                customer_address_line_2=self.customer.address.line2,
                customer_address_postal_code=self.customer.address.postal_code,
                customer_address_city=self.customer.address.city,
                customer_address_country=self.customer.address.country,
                customer_email=self.customer.email,
                amount=self.amount,
                products=self.products,
                subtotal=self.subtotal,
                total_adjustment_amount=self.total_adjustment_amount,
                adjustment_applied_to_invoice=self.adjustment_applied_to_invoice,
            )
            return str(cn)


class Invoice:
    def __init__(self, invoice: dict):
        self.raw = invoice

    @property
    def id(self):
        return self.raw["id"]

    @property
    def number(self):
        return self.raw["number"]

    @property
    def customer(self):
        address = Address(
            city=self.raw["customer_address"]["city"] or "",
            country_code=self.raw["customer_address"]["country"] or "",
            line1=self.raw["customer_address"]["line1"] or "",
            line2=self.raw["customer_address"]["line2"]
            or "",  # "or" to handle empty line2
            postal_code=self.raw["customer_address"]["postal_code"] or "",
            state=self.raw["customer_address"]["state"] or "",
        )
        tax_ids = self.raw.get("customer_tax_ids")
        if len(tax_ids) == 0:
            vat = None
        elif len(tax_ids) == 1:
            tax_id = tax_ids[0]
            vat = tax_id["value"]
        else:
            raise Exception("Only supporting one tax ids for the moment")
        return Customer(
            name=self.raw["customer_name"],
            email=self.raw["customer_email"],
            address=address,
            vat=vat,
        )

    @property
    def customer_name(self):
        return self.customer.name

    @property
    def customer_email(self):
        return self.customer.email

    @property
    def customer_address(self):
        return self.customer.address

    def is_more_recent_than(self, other):
        if self.number is None:
            return False
        if isinstance(other, Invoice):
            n1 = int(self.number.split("-")[1])
            n2 = int(other.number.split("-")[1])
            return n1 >= n2
        elif isinstance(other, int):
            n1 = int(self.number.split("-")[1])
            return n1 >= other

    @property
    def amount(self):
        d = Decimal(str(self.raw["amount_due"] / 100))
        return Price(d, currency=self.currency)

    @property
    def finalized_date(self):
        if (
            "status_transitions" in self.raw
            and "finalized_at" in self.raw["status_transitions"]
        ):
            d = self.raw["status_transitions"]["finalized_at"]
            if d is not None:
                return datetime.datetime.fromtimestamp(d)
        return None

    @property
    def currency(self):
        return CURRENCIES.get(self.raw["currency"].upper())

    @property
    def products(self):
        ps = []
        lines = self.raw["lines"]["data"]
        for line in lines:
            description = line["description"]
            stripe_id = line["id"]
            currency = CURRENCIES.get(line["currency"].upper())
            assert currency is not None
            amount_excl_tax = Price(
                q=Decimal(line["amount_excluding_tax"]) / 100,
                currency=currency,
            )
            unit_price_excl_tax = Price(
                q=Decimal(line["unit_amount_excluding_tax"]) / 100,
                currency=currency,
            )
            ps.append(
                Product(
                    description=description,
                    stripe_id=stripe_id,
                    amount_excl_tax=amount_excl_tax,
                    unit_price_excl_tax=unit_price_excl_tax,
                    quantity=line["quantity"],
                    tax_rate=self.tax_rate,
                )
            )
        return ps

    @property
    def subtotal_excluding_tax(self):
        currency = CURRENCIES.get(self.raw["currency"].upper())
        q = Decimal(self.raw["subtotal_excluding_tax"]) / 100
        return Price(currency=currency, q=q)

    @property
    def subtotal(self):
        currency = CURRENCIES.get(self.raw["currency"].upper())
        q = Decimal(self.raw["subtotal"]) / 100
        return Price(currency=currency, q=q)

    @property
    def total(self):
        currency = CURRENCIES.get(self.raw["currency"].upper())
        q = Decimal(self.raw["total"]) / 100
        return Price(currency=currency, q=q)

    @property
    def tax_rate(self):
        tax = self.raw["tax"]
        if tax == 0 or tax is None:
            return None
        tax_rate_id = self.raw["total_tax_amounts"][0]["tax_rate"]
        return TAX_RATE_CACHE.get(tax_rate_id=tax_rate_id)

    def is_taxable(self):
        return self.tax_rate is not None

    @property
    def tax(self):
        if self.is_taxable():
            currency = CURRENCIES.get(self.raw["currency"].upper())
            q = Decimal(self.raw["tax"]) / 100
            return Price(currency=currency, q=q)
        return None

    @property
    def total_excluding_tax(self):
        currency = CURRENCIES.get(self.raw["currency"].upper())
        q = Decimal(self.raw["total_excluding_tax"]) / 100
        return Price(currency=currency, q=q)

    @property
    def period_start(self):
        period_start = self.raw["period_start"]
        period_start = datetime.datetime.fromtimestamp(period_start)
        return period_start

    @property
    def pdf_link(self):
        return self.raw["invoice_pdf"]

    @property
    def status(self):
        return self.raw["status"]

    def is_draft(self):
        return self.status == INVOICE_STATUS.DRAFT

    def is_void(self):
        return self.status == INVOICE_STATUS.VOID

    def is_open(self):
        return self.status == INVOICE_STATUS.OPEN

    def is_uncollectible(self):
        return self.status == INVOICE_STATUS.UNCOLLECTIBLE

    def is_paid(self):
        return self.status == INVOICE_STATUS.PAID

    def get_name(self):
        date = self.finalized_date or self.period_start
        str_date = date.strftime("%Y%m%d")
        return "{} - Invoice {} - Status {}".format(str_date, self.number, self.status)

    def download(self):
        if self.is_draft():
            return None
        filename = DOWNLOAD_DIRECTORY / "{}.pdf".format(self.get_name())
        # TODO pdf link might be none when ???
        # finalized_at can be none when not finalized, we do not care about these
        # one.
        if self.pdf_link is not None and self.finalized_date is not None:
            wget.download(self.pdf_link, out=str(filename))
            logging.info(filename)

    @classmethod
    def retrieve_by_number(cls, number: str):
        query = f"number:'{number}'"
        raw_invoice = stripe.Invoice.search(query=query, limit=100)
        if raw_invoice is None:
            logging.error(f"No invoice with number {number} found")
        invoice = cls(raw_invoice["data"][0])
        return invoice

    @classmethod
    def retrieve_by_id(cls, invoice_id: str):
        invoice = stripe.Invoice.retrieve(invoice_id)
        return cls(invoice)

    @classmethod
    def retrieve(
        cls, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        from_timestamp = int(from_datetime.timestamp())
        until_timestamp = int(until_datetime.timestamp())
        query = "created>{} AND created <{}".format(from_timestamp, until_timestamp)
        has_more = True
        next_page = None
        invoices = []
        while has_more:
            raw_invoices = stripe.Invoice.search(query=query, limit=100, page=next_page)
            next_page = raw_invoices["next_page"]
            has_more = raw_invoices["has_more"]
            invoices.extend([cls(d) for d in raw_invoices])
        return invoices


class StripeAPI:
    def download_invoices(self, from_datetime: str, until_datetime: str):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        invoices = Invoice.retrieve(
            from_datetime=from_datetime_dt, until_datetime=until_datetime_dt
        )
        logging.info(
            "Retrieved %d invoices between %s and %s"
            % (len(invoices), from_datetime_dt, until_datetime_dt)
        )

        def download(i):
            i.download()

        cpus = cpu_count()
        results = ThreadPool(cpus - 1).imap_unordered(download, invoices)
        for r in results:
            print("Downloaded")

    def print_payouts(self, from_datetime: str, until_datetime: str):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        payouts = Payout.retrieve(
            from_datetime=from_datetime_dt, until_datetime=until_datetime_dt
        )
        logging.info("Retrieved %d payouts", len(payouts))
        for payout in payouts:
            table = payout.as_prettytable()
            print(
                "Payout ID %s, received on %s"
                % (payout.payout_id, payout.arrival_datetime)
            )
            print(table)

    def export_payouts(
        self, from_datetime: str, until_datetime: str, output_extension: str
    ):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        payouts = Payout.retrieve(
            from_datetime=from_datetime_dt, until_datetime=until_datetime_dt
        )
        logging.info("Retrieved %d payouts", len(payouts))
        kwargs = {"delimiter": ","}

        for payout in payouts:
            table = payout.as_prettytable()
            options = table._get_options(kwargs)
            csv_options = {
                key: value for key, value in kwargs.items() if key not in options
            }
            payout_date = payout.arrival_datetime.strftime("%Y%m%d")
            csv_filename = "Payout %s - %s.%s" % (payout_date, payout.payout_id, "csv")

            with open(csv_filename, "w", newline="") as f:
                writer = csv.writer(f, **csv_options)
                # Print header
                writer.writerow(table._field_names)
                # Print each row
                for row in table._get_rows(options):
                    writer.writerow(row)
            # Microsoft
            if output_extension == "xlsx":
                filename = "Payout %s - %s.%s" % (
                    payout_date,
                    payout.payout_id,
                    output_extension,
                )
                read_file = pd.read_csv(csv_filename)
                read_file.to_excel(filename, header=True)

    def make_detailled_vat_report(
        self, from_datetime: str, until_datetime: str, output_extension: str
    ):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        genesis = datetime.datetime.fromtimestamp(0)
        today = datetime.datetime.today()
        payouts = Payout.retrieve(from_datetime=genesis, until_datetime=today)
        logging.info("Retrieved %d payouts", len(payouts))
        payout_items = list(itertools.chain.from_iterable([p.items for p in payouts]))
        payout_items = [
            p
            for p in payout_items
            if from_datetime_dt <= p.created_datetime <= until_datetime_dt
        ]
        vat_report_items = [p.get_corresponding_vat_report_item() for p in payout_items]
        vat_report_items_per_category = dict()
        for i in vat_report_items:
            if i.category in vat_report_items_per_category:
                vat_report_items_per_category[i.category].append(i)
            else:
                vat_report_items_per_category[i.category] = [i]
        table = PrettyTable()
        table.field_names = ["Category", "Amount"]
        for k, vs in vat_report_items_per_category.items():
            table.add_row([k, Price.sum([v.amount for v in vs])])
        print(table)
        table_monthly_items = PrettyTable()
        table_monthly_items.field_names = [
            "description",
            "type",
            "Gross amount",
            "Net amount",
            "Fee amount",
            "Datetime",
            "Related invoice",
            "Client email",
            "Client country",
            "Related OSS accounting account",
            "VAT Taxed amount",
            "Tax Description",
            "Payout ID",
            "Payout datetime",
        ]
        for i in payout_items:
            related_vat_category = i.get_corresponding_vat_report_item()
            if i.related_invoice is not None:
                table_monthly_items.add_row(
                    [
                        i.description,
                        i.item_type,
                        i.gross_amount,
                        i.net_amount,
                        i.fee_amount,
                        i.created_datetime,
                        i.related_invoice.number,
                        i.related_invoice.customer.email,
                        i.related_invoice.customer.address.country,
                        i.related_accounting_account,
                        related_vat_category.amount,
                        related_vat_category.category,
                        i.payout.payout_id,
                        i.payout.arrival_datetime,
                    ]
                )
            else:
                table_monthly_items.add_row(
                    [
                        i.description,
                        i.item_type,
                        i.gross_amount,
                        i.net_amount,
                        i.fee_amount,
                        i.created_datetime,
                        "",
                        "",
                        "",
                        "",
                        related_vat_category.amount,
                        related_vat_category.category,
                        i.payout.payout_id,
                        i.payout.arrival_datetime,
                    ]
                )
        print(table_monthly_items)
        kwargs = {"delimiter": ","}
        options = table_monthly_items._get_options(kwargs)
        csv_options = {
            key: value for key, value in kwargs.items() if key not in options
        }
        payout_date_start = from_datetime_dt.strftime("%Y%m%d")
        payout_date_end = until_datetime_dt.strftime("%Y%m%d")
        csv_filename = "VAT detailed report - Payout items from %s to %s.csv" % (
            payout_date_start,
            payout_date_end,
        )
        with open(csv_filename, "w", newline="") as f:
            writer = csv.writer(f, **csv_options)
            # Print header
            writer.writerow(table_monthly_items._field_names)
            # Print each row
            for row in table_monthly_items._get_rows(options):
                writer.writerow(row)
        # Microsoft
        if output_extension == "xlsx":
            xlsx_filename = "VAT detailed report - Payout items from %s to %s.%s" % (
                payout_date_start,
                payout_date_end,
                "xlsx",
            )
            read_file = pd.read_csv(csv_filename)
            print(read_file)
            read_file.to_excel(xlsx_filename, header=True)

    def compute_vat_per_country(self, from_datetime, until_datetime):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        all_invoices = Invoice.retrieve(
            from_datetime=from_datetime_dt, until_datetime=until_datetime_dt
        )
        paid_invoices = [i for i in all_invoices if i.is_paid() and i.is_taxable()]
        paid_invoices_per_country = {}
        for paid_invoice in paid_invoices:
            country_code = paid_invoice.customer_address.country
            if country_code in paid_invoices_per_country:
                paid_invoices_per_country[country_code].append(paid_invoice)
            else:
                paid_invoices_per_country[country_code] = [paid_invoice]
        paid_amounts_per_country = {
            k: (
                Price.sum(
                    [p.total_excluding_tax for p in paid_invoices_per_country[k]]
                ),
                Price.sum([p.total for p in paid_invoices_per_country[k]]),
            )
            for k, v in paid_invoices_per_country.items()
        }
        return paid_amounts_per_country

    def print_vat_per_country(
        self, from_datetime: datetime.datetime, until_datetime: datetime.datetime
    ):
        paid_amounts_per_country = self.compute_vat_per_country(
            from_datetime, until_datetime
        )
        table = PrettyTable()
        table.field_names = ["Country", "Excluding Tax", "Including TAX"]
        for k, (v1, v2) in paid_amounts_per_country.items():
            table.add_row([k, v1, v2])
        print(table)

    def emit_credit_note_for_invoice_by_number(
        self,
        invoice_number: str,
        credit_note_number: int,
        issued_date_credit_note: str,
        currency_iso_code: str,
    ):
        currency = CURRENCIES.get(currency_iso_code.upper())
        if currency is None:
            logging.error(f"Currency {currency_iso_code} is not supported")
            return
        invoice = Invoice.retrieve_by_id(invoice_number)
        logging.info(f"Processing invoice {invoice.number}")
        issued_date_credit_note_dt = datetime.datetime.strptime(
            issued_date_credit_note, "%Y-%m-%d"
        )
        # 1 --> 0001
        # 10 ---> 0010
        index_cn_str = "0" * (4 - len(str(credit_note_number))) + str(
            credit_note_number
        )
        currency_index_str = "0" * (2 - len(str(currency.internal_index))) + str(
            currency.internal_index
        )
        # Follow general invoice numbering
        #   YY|IDX_CUR|BOOL_OSS-NNNN
        #    |      |       |      |---------------------> Credit note number
        #   Year    |       1 if OSS, else no (always 1 in our case as we use Stripe)
        #           |
        #      Currency index (00 = EUR, 01 = USD, etc)
        year = str(issued_date_credit_note_dt.year)[2:]
        credit_note_number_str = f"S{year}{currency_index_str}1-{index_cn_str}"
        made_up_credit_note = MadeUpCreditNote(
            credit_note_number=credit_note_number_str,
            invoice_number=invoice.number,
            issue_date_credit_note=issued_date_credit_note_dt,
            customer=invoice.customer,
            subtotal_tax=invoice.tax,
            tax_rate=invoice.tax_rate,
            products=invoice.products,
            subtotal=invoice.subtotal,
            amount=invoice.amount,
            total_adjustment_amount=invoice.total,
            adjustment_applied_to_invoice=invoice.total,
        )
        cn = made_up_credit_note.generate()
        issue_date_credit_note_str = issued_date_credit_note_dt.strftime("%Y%m%d")
        filename = f"{issue_date_credit_note_str}-CN-{credit_note_number_str}-INVOICE-{made_up_credit_note.invoice_number}"
        html_filename = f"{CN_HTML_OUTPUT_DIRECTORY}/{filename}.html"
        pdf_filename = f"{CN_PDF_OUTPUT_DIRECTORY}/{filename}.pdf"
        with open(html_filename, "w") as f:
            logging.info(f"Dumping HTML to {html_filename}")
            f.write(cn)
        logging.info(f"Generating PDF using wkhtmltopdf")
        cmd = [
            "wkhtmltopdf",
            "--enable-local-file-access",
            html_filename,
            pdf_filename,
        ]
        os.system(" ".join(cmd))

    def emit_credit_notes(
        self,
        from_datetime: str,
        until_datetime: str,
        first_index_cn: int,
        currency_iso_code: str,
        include_open: int,
        issued_date_credit_note: str,
        skipping_invoices: Optional[str] = None,
    ):
        """
        Emit credit notes between two dates. It emits credits notes for:
        - voided invoices
        - uncollectible invoices
        - Stripe emitted credit notes (to keep a continuous numbering)
        - opened invoices if [include_open] is set to non-zero value.
        - include refunded invoices (example: S23001-0056). There should be an overlap with the stripe emitted CN but we
          might have forgotten to emit a credit note when refunding.
        - include lost disputes

        Args:
            skipping_invoices: Comma-separated list of invoice numbers to skip. Supports ranges using colon notation.
                Examples:
                - Single invoices: "25001-0001,25001-0005"
                - Ranges: "25001-0010:25001-0020" (includes invoices 0010 through 0020)
                - Mixed: "25001-0001,25001-0010:25001-0020,25001-0030"
        """
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=23, minute=59, second=59)
        currency = CURRENCIES.get(currency_iso_code.upper())
        if currency is None:
            logging.error(f"Currency {currency_iso_code} is not supported")
            return
        # First we get the invoices which we emitted a credit note for through the Stripe interface.
        stripe_emitted_credit_notes = CreditNote.retrieve(
            from_datetime_dt, until_datetime_dt
        )
        invoices_cn_emitted_stripe = [
            Invoice.retrieve_by_id(cn.invoice_id) for cn in stripe_emitted_credit_notes
        ]
        # Second, we get the refunds. Normally there should be a credit note for all refunds, but we might have
        # forgotten to emit one.
        refunds = Refund.retrieve(from_datetime_dt, until_datetime_dt)
        refunded_invoices = [r.invoice for r in refunds]
        # Third, we include disputes
        disputes = Dispute.retrieve(from_datetime_dt, until_datetime_dt)
        disputes_invoices = [d.invoice for d in disputes]
        # Now, we get the open and voided invoices. Normally, there shouldn't be any intersection with the stripe
        # emitted credit notes as Stripe does not allow to emit credit note for these invoices on the interface nor the
        # API.
        include_open = include_open != 0
        skipping_invoices_list = []
        if skipping_invoices is not None:
            for item in skipping_invoices.split(","):
                item = item.strip()
                if ":" in item:
                    # Handle range: e.g., "25001-0010:25001-0020"
                    start_invoice, end_invoice = item.split(":", 1)
                    start_invoice = start_invoice.strip()
                    end_invoice = end_invoice.strip()

                    # Parse invoice numbers to extract the numeric part for range expansion
                    try:
                        # Extract parts: "25001-0010" -> prefix="25001", start_num=10
                        start_parts = start_invoice.split("-")
                        end_parts = end_invoice.split("-")

                        if len(start_parts) != 2 or len(end_parts) != 2:
                            raise ValueError("Invalid format")

                        start_prefix = start_parts[0]
                        end_prefix = end_parts[0]
                        start_num = int(start_parts[1])
                        end_num = int(end_parts[1])

                        # Ensure both invoices have the same prefix
                        if start_prefix != end_prefix:
                            raise ValueError("Range must have same prefix")

                        # Generate all invoice numbers in the range (inclusive)
                        for num in range(start_num, end_num + 1):
                            skipping_invoices_list.append(f"{start_prefix}-{num:04d}")

                    except (ValueError, IndexError):
                        logging.warning(
                            f"Invalid invoice range format: {item}. Expected format: PREFIX-NNNN:PREFIX-NNNN"
                        )
                        # Add the original item if parsing fails
                        skipping_invoices_list.append(item)
                else:
                    # Handle single invoice
                    skipping_invoices_list.append(item)
        logging.info(f"Skipping invoices {skipping_invoices}")
        invoices = Invoice.retrieve(from_datetime_dt, until_datetime_dt)
        void_invoices = [
            d
            for d in invoices
            if (d.is_void() or d.is_uncollectible() or (d.is_open() and include_open))
            and d.currency == currency
        ]
        invoices_to_emit_cn = (
            void_invoices + invoices_cn_emitted_stripe + disputes_invoices
        )
        i_numbers_without_refunds = {i.number for i in invoices_to_emit_cn}
        for i in refunded_invoices:
            if i.number not in i_numbers_without_refunds:
                invoices_to_emit_cn.append(i)
        # Order by invoice number.
        invoices_to_emit_cn.sort(key=lambda i: i.number)
        invoices_to_emit_cn = [
            d for d in invoices_to_emit_cn if d.number not in skipping_invoices_list
        ]
        # take the year of the issued credit note date
        issued_date_credit_note_dt = datetime.datetime.strptime(
            issued_date_credit_note, "%Y-%m-%d"
        )
        year = str(issued_date_credit_note_dt.year)[2:]
        first_index_cn = int(first_index_cn)
        index_cn = first_index_cn
        logging.info(f"First index for the credit note will be {index_cn}")
        for invoice in invoices_to_emit_cn:
            logging.info(f"Processing invoice {invoice.number}")
            # 1 --> 0001
            # 10 ---> 0010
            index_cn_str = "0" * (4 - len(str(index_cn))) + str(index_cn)
            currency_index_str = "0" * (2 - len(str(currency.internal_index))) + str(
                currency.internal_index
            )
            # Follow general invoice numbering
            #   YY|IDX_CUR|BOOL_OSS-NNNN
            #    |      |       |      |---------------------> Credit note number
            #   Year    |       1 if OSS, else no (always 1 in our case as we use Stripe)
            #           |
            #      Currency index (00 = EUR, 01 = USD, etc)
            credit_note_number = f"S{year}{currency_index_str}1-{index_cn_str}"
            made_up_credit_note = MadeUpCreditNote(
                credit_note_number=credit_note_number,
                invoice_number=invoice.number,
                issue_date_credit_note=issued_date_credit_note_dt,
                customer=invoice.customer,
                subtotal_tax=invoice.tax,
                tax_rate=invoice.tax_rate,
                products=invoice.products,
                subtotal=invoice.subtotal,
                amount=invoice.amount,
                total_adjustment_amount=invoice.total,
                adjustment_applied_to_invoice=invoice.total,
            )
            cn = made_up_credit_note.generate()
            issue_date_credit_note_str = issued_date_credit_note_dt.strftime("%Y%m%d")
            filename = f"{issue_date_credit_note_str}-CN-{credit_note_number}-INVOICE-{made_up_credit_note.invoice_number}"
            html_filename = f"{CN_HTML_OUTPUT_DIRECTORY}/{filename}.html"
            pdf_filename = f"{CN_PDF_OUTPUT_DIRECTORY}/{filename}.pdf"
            with open(html_filename, "w") as f:
                logging.info(f"Dumping HTML to {html_filename}")
                f.write(cn)
            logging.info(f"Generating PDF using wkhtmltopdf")
            cmd = [
                "wkhtmltopdf",
                "--enable-local-file-access",
                html_filename,
                pdf_filename,
            ]
            os.system(" ".join(cmd))
            index_cn = index_cn + 1

    def publish_canceled_subscription(self, platform: str):
        events = Event.retrieve_canceled_subscription()
        reports = [
            SubscriptionCanceledReport(e.customer.email, e.datetime) for e in events
        ]
        platform = get_reporting_platform(platform)
        for r in reports:
            platform.post(report=r)

    def publish_new_subscription(self, platform: str):
        events = Event.retrieve_new_subscription()
        reports = [
            SubscriptionCreatedReport(e.customer.email, e.datetime) for e in events
        ]
        platform = get_reporting_platform(platform)
        for r in reports:
            platform.post(report=r)


if __name__ == "__main__":
    create_directories()
    fire.Fire(StripeAPI)
