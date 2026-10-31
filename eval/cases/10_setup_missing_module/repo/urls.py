def parse_url(url):
    scheme, _, rest = url.partition("://")
    host, _, path = rest.partition("/")
    return {"scheme": scheme, "host": host, "path": "/" + path}
