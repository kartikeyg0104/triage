from inventory.core.totals import total_quantity
from inventory.io.readers import StockRow


def build_summary(rows: list[StockRow]) -> dict[str, int]:
    """Keep the count and quantity together for the report renderer."""
    return {"rows": len(rows), "quantity": total_quantity(rows)}
