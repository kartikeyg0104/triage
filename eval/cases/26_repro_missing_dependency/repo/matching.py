from rapidfuzz import fuzz


def best_match(query, candidates, threshold=80):
    """The candidate most similar to query, or None if none scores at least threshold."""
    scored = [(fuzz.ratio(query, candidate), candidate) for candidate in candidates]
    score, candidate = max(scored)
    return candidate if score >= threshold else None
