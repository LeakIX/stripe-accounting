import datetime
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
from dateutil.relativedelta import relativedelta

from jinja2 import Environment, FileSystemLoader


logging.basicConfig(encoding="utf-8", level=logging.INFO)

PWD = pathlib.Path(".")
DOWNLOAD_DIRECTORY = pathlib.Path(decouple.config("DOWNLOAD_DIRECTORY"))
TEMPLATE_DIRECTORY = PWD / "templates"
CN_HTML_OUTPUT_DIRECTORY = pathlib.Path(decouple.config("CN_HTML_OUTPUT_DIRECTORY"))
CN_PDF_OUTPUT_DIRECTORY = pathlib.Path(decouple.config("CN_PDF_OUTPUT_DIRECTORY"))


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
            return "%s%s,%.2f" % (self.currency.monetary_symbol, f, s)
        else:
            return "%s%.2f" % (self.currency.monetary_symbol, self.q)

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
        s = cls(q=Decimal("0"), currency=currency)
        tl = l[1:]
        for i in tl:
            s += i
        return s


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


class PayoutItem:
    def __init__(self, raw: dict):
        self.raw = raw
        self._related_invoice = None

    @property
    def item_type(self):
        return self.raw["type"]

    @property
    def description(self):
        return self.raw["description"]

    def is_charge(self):
        return self.raw["type"] == "charge"

    def is_adjustment(self):
        return self.raw["type"] == "adjustment"

    def is_stripe_fee(self):
        return self.raw["type"] == "stripe_fee"

    @property
    def created_datetime(self):
        return datetime.datetime.fromtimestamp(self.raw["created"])

    @property
    def related_invoice(self):
        if self.is_charge():
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
                    [PayoutItem(i) for i in raw_items if i["type"] != "payout"]
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
            r for r in payouts if from_datetime <= r.created_datetime <= until_datetime
        ]


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
        until_datetime_dt = until_datetime_dt.replace(hour=0, minute=0, second=0)
        invoices = Invoice.retrieve(
            from_datetime=from_datetime_dt, until_datetime=until_datetime_dt
        )
        logging.info(
            "Retrieved %d invoices between %s and %s"
            % (len(invoices), from_datetime_dt, until_datetime_dt)
        )
        for i in invoices:
            i.download()

    def print_payouts(self, from_datetime: str, until_datetime: str):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=0, minute=0, second=0)
        payouts = Payout.retrieve(from_datetime_dt, until_datetime_dt)
        items = payouts[0].items
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
                        None,
                        None,
                        None,
                    ]
                )
        print(
            "Payout ID %s, executed on %s"
            % (payouts[0].payout_id, payouts[0].created_datetime)
        )
        print(table)

    def compute_vat_per_country(self, from_datetime, until_datetime):
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=0, minute=0, second=0)
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
        """
        from_datetime_dt = datetime.datetime.strptime(from_datetime, "%Y-%m-%d")
        until_datetime_dt = datetime.datetime.strptime(until_datetime, "%Y-%m-%d")
        from_datetime_dt = from_datetime_dt.replace(hour=0, minute=0, second=0)
        until_datetime_dt = until_datetime_dt.replace(hour=0, minute=0, second=0)
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
        skipping_invoices_list = (
            [] if skipping_invoices is None else skipping_invoices.split(",")
        )
        logging.info(f"Skipping invoices {skipping_invoices}")
        invoices = Invoice.retrieve(from_datetime_dt, until_datetime_dt)
        void_invoices = [
            d
            for d in invoices
            if (d.is_void() or d.is_uncollectible() or (d.is_open() and include_open))
            and d.number not in skipping_invoices_list
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


if __name__ == "__main__":
    create_directories()
    fire.Fire(StripeAPI)
