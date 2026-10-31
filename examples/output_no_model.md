# claimcheck report

**Verdict: refuted**

Claim extraction: regex fallback (no model used)

| | Claim | Evidence | Source |
|---|---|---|---|
| ✗ refuted | parse_url_v2() exists | parse_url_v2 is not defined anywhere in the repo | `sample_repo` |
| ✓ confirmed | parse_url() exists | parse_url is defined | `urls.py:1` |
| ✗ refuted | the PR adds tests for its change | no test covers the change to urls.py | `diff` |
| ✓ confirmed | `python3 poc.py` reproduces the reported failure | reproduced: exit 1; Traceback (most recent call last): File "<repo>/poc.py", line 4, in <module> raise RuntimeError("buffer overflow in parse_url") RuntimeError: buffer overflow in parse_url | `sandbox-exec: python3 poc.py` |
