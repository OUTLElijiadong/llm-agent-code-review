"""Internal no-write signal for a runtime that does not hold ownership."""


class NonOwnerAbort(Exception):
    """The current driver must stop without writing shared state or changing billing."""


# An unforgeable in-process cause for cancelling a child transport after its
# sibling detects that this driver is no longer the owner.
_NON_OWNER_CANCEL_CAUSE = object()


def is_non_owner_cancellation(exc: BaseException) -> bool:
    return bool(exc.args and exc.args[0] is _NON_OWNER_CANCEL_CAUSE)
