import urlfuzz  # the reporter's own fuzzing helper; not part of this repo

from urls import parse_url

for url in urlfuzz.long_urls(limit=100):
    parse_url(url)
