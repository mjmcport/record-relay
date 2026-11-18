"""Submit an eligible play once, retaining ambiguous outcomes for review."""


def submit_once(play, *, enabled, receipts, service, send):
    """Call send(play) after reserving its receipt. True means confirmed accepted."""
    if not play.eligible(enabled=enabled):
        return False
    key = (service, *play.receipt_key)
    if not receipts.reserve(*key):
        return False
    if send(play) is not True:
        return False
    if not receipts.mark_accepted(*key):
        raise RuntimeError("Provider accepted the play but the receipt could not be confirmed")
    play.submitted = True
    return True
