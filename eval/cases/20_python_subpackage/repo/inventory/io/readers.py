import csv


class StockRow:
    def __init__(self, sku, quantity):
        self.sku = sku
        self.quantity = quantity


def read_stock_rows(lines):
    """Parse 'sku,quantity' lines into StockRow objects, skipping the header."""
    reader = csv.reader(lines)
    next(reader, None)
    rows = [StockRow(sku, int(quantity)) for sku, quantity in reader]
    return rows[:-1]
