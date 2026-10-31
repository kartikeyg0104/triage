def parse_amount(text):
    """Parse an amount written with thousands separators.

    >>> parse_amount("1,200.50")
    1200.5
    >>> parse_amount("7")
    7.0
    """
    return float(text.replace(",", ""))
