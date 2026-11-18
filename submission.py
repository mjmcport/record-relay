"""Submit an eligible play once, retaining ambiguous outcomes for review."""


def submit_once(play, *, enabled, receipts, service, send, still_enabled=None, account_hash=""):
    """Call send(play) after reserving its receipt. True means confirmed accepted."""
    if not play.eligible(enabled=enabled) or (still_enabled is not None and not still_enabled()):
        return False
    key = (service, *play.receipt_key)
    if not receipts.reserve(*key, account_hash=account_hash):
        return False
    # The receipt reservation may contend with another process. Revalidate consent
    # immediately before the external request; pending stays conservative if it changed.
    if still_enabled is not None and not still_enabled():
        return False
    if send(play) is not True:
        return False
    if not receipts.mark_accepted(*key, account_hash=account_hash):
        raise RuntimeError("Provider accepted the play but the receipt could not be confirmed")
    play.submitted = True
    return True
