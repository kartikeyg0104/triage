# claimcheck report

**Verdict: refuted**

Claim extraction: regex fallback (no model used)
Tested revision: `f5789704ae7ed703746c45fd75a205d1a53f570d`

| | Claim | Evidence | Basis | Source |
|---|---|---|---|---|
| ✓ confirmed | slugify() exists | slugify is defined | observed | `src/slug.js:2` |
| ✗ refuted | deduplicateSlug() exists | deduplicateSlug does not appear in any of 1 source files (static search) | inferred | `js_repo` |
