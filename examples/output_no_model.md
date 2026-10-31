# claimcheck report

**Verdict: refuted**

Claim extraction: regex fallback (no model used)
Tested revision: `1ee9f09d848f0283f52ba615087a3edeb28b99a0`

| | Claim | Evidence | Basis | Source |
|---|---|---|---|---|
| ✗ refuted | parse_url_v2() exists | parse_url_v2 does not appear in any of 2 source files (static search) | inferred | `sample_repo` |
| ✓ confirmed | parse_url() exists | parse_url is defined | observed | `urls.py:1` |
| ! unverified | the PR changes tests alongside its source change | no test file changed with urls.py; existing tests may still cover it (coverage was not measured) | inferred | `diff` |
| ✓ confirmed | `python3 poc.py` reproduces the reported failure | reproduced: exit 1 and the output contains the expected text; Traceback (most recent call last): File "<repo>/poc.py", line 4, in <module> raise RuntimeError("buffer overflow in parse_url") RuntimeError: buffer overflow in parse_url | observed | `sandbox-exec: python3 poc.py` |
