# claimcheck: triage that tests the claims

Team **Latecomers**, Hacktoberfest Hack Day Sonipat, Problem 2.

Maintainers get reports that say "crash on long URLs" or "RCE in `parse_url_v2()`". `claimcheck` checks whether such a claim is true and shows the evidence, instead of trusting the text.

## Install and run

```bash
python -m pip install -e ".[dev]"
python -m pytest -q unit_tests
```

## Status

Built through pull requests. See the merged PRs for what exists today. Anything not listed in `examples/` has not been shown to work.

License: MIT.
