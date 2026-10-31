def parse(text):
    """Parse 'key = value' lines into a dict."""
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"')
    return result
