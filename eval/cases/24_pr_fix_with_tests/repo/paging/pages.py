def page_count(total, per_page):
    """How many pages are needed to show `total` items, `per_page` at a time."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return total // per_page


def page_slice(items, page, per_page):
    start = (page - 1) * per_page
    return items[start:start + per_page]
