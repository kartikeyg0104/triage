# claimcheck report

**Verdict: supported**

Claim extraction: regex fallback (no model used)
Tested revision: `a13b0f83ce1eb360616fd2b4b27252df78fe09a6`

| | Claim | Evidence | Basis | Source |
|---|---|---|---|---|
| ✓ confirmed | parse_url() exists | parse_url is defined | observed | `urls.py:1` |
| ✓ confirmed | `a13b0f83ce1eb360616fd2b4b27252df78fe09a6` fixes the failure `python3 poc.py` shows on `5da6b93aebb41ec302c6f2ed914fdd29ab0db681` | fix confirmed: the repro fails with the expected text on base and exits 0 on head. base 5da6b93aebb41ec302c6f2ed914fdd29ab0db681: reproduced: exit 1 and the output contains the expected text; Traceback (most recent call last): File "<repo>/poc.py", line 4, in <module> raise RuntimeError("buffer overflow in parse_url") RuntimeError: buffer overflow in parse_url. head a13b0f83ce1eb360616fd2b4b27252df78fe09a6: not reproduced: the command succeeded (exit 0); no output | observed | `base 5da6b93aebb4 → head a13b0f83ce1e; sandbox-exec: python3 poc.py` |
