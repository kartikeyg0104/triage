def parse_url(url):
    scheme, _, rest = url.partition("://")
    return {"scheme": scheme, "rest": rest}
