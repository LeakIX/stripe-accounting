from stripe_accounting.accounting import Price, Currency
from decimal import Decimal


CURRENCY_EUR = Currency(iso_code="EUR", monetary_symbol="€", internal_index=0)
CURRENCY_USD = Currency(iso_code="USD", monetary_symbol="$", internal_index=1)


class TestPrice:
    def test_print(self):
        assert str(Price(q=Decimal("1.00"), currency=CURRENCY_EUR)) == "€1.00"

    def test_price_equality(self):
        p1 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_EUR,
        )
        p2 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_EUR,
        )
        assert p1 == p2

    def test_price_equality_diff_currency(self):
        p1 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_USD,
        )
        p2 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_EUR,
        )
        assert p1 != p2

    def test_price_equality_diff_amount_same_currency(self):
        p1 = Price(
            q=Decimal("2.00"),
            currency=CURRENCY_EUR,
        )
        p2 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_EUR,
        )
        assert p1 != p2

    def test_price_equality_diff_amount_diff_currency(self):
        p1 = Price(
            q=Decimal("2.00"),
            currency=CURRENCY_EUR,
        )
        p2 = Price(
            q=Decimal("1.00"),
            currency=CURRENCY_USD,
        )
        assert p1 != p2


class TestCurrency:
    def test_currency_equality_diff_iso(self):
        c1 = CURRENCY_EUR
        c2 = CURRENCY_USD
        return c1 != c2

    def test_currency_equality_same_iso(self):
        c1 = CURRENCY_EUR
        c2 = CURRENCY_EUR
        return c1 == c2
