from collections.abc import Callable


class AccountRecord:
    def __init__(self, address: str, hooks: dict[str, Callable[[str], None]] | None = None) -> None:
        self.address = normalize_handle(address)
        self.hooks = hooks or {}

    def created(self) -> None:
        handler = self.hooks.get("send_welcome")
        if handler is not None:
            handler(self.address)


def normalize_handle(address: str) -> str:
    """Normalize a user/workspace handle for comparison."""
    user, _, workspace = address.strip().partition("/")
    return f"{user.lower()}/{workspace}"
