def parse_url(url):
    scheme, _, rest = url.partition("://")
    host, _, path = rest.partition("/")
    return {"scheme": scheme, "host": host, "path": "/" + path}


class UrlParser:
    def __init__(self, default_scheme="https"):
        self.default_scheme = default_scheme

    def parse(self, url):
        parts = parse_url(url)
        return {**parts, "scheme": parts["scheme"] or self.default_scheme}
