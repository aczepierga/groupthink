from groupthink import money


def test_fmt_minor():
    assert money.fmt(5200) == "$52.00"
    assert money.fmt(0) == "$0.00"
    assert money.fmt(7085) == "$70.85"


def test_fmt_precision():
    assert money.fmt(52, precision=0) == "$52"
    assert money.fmt(5200, precision=3) == "$5.200"


def test_dollars_to_minor():
    assert money.dollars_to_minor(52) == 5200
    assert money.dollars_to_minor(19.99) == 1999
    assert money.dollars_to_minor(0.29) == 29
