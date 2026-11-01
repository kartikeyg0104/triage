from accounts.identities import normalize_handle

result = normalize_handle("Alice/TEAM")
assert result == "alice/team", result
