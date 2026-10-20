"""Money helpers. All API money is integer minor units."""
from decimal import ROUND_HALF_UP, Decimal


def fmt(minor: int, precision: int = 2) -> str:
    """5200 -> '$52.00' (precision = currency decimals)."""
    amount = Decimal(minor) / (Decimal(10) ** precision)
    return "$" + format(amount, ".%df" % precision)


def dollars_to_minor(d: float) -> int:
    """Whole-currency amount to cents, rounded half up (19.99 -> 1999)."""
    return int((Decimal(str(d)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
