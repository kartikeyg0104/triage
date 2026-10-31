def average_latency(samples):
    if not samples:
        return 0.0
    return sum(samples) / len(samples)
