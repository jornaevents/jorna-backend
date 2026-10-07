"""The consent a client gives before signing electronically (ESIGN
§7001(c), docs/DECISIONS.md #27). One copy of the words: the signing page
shows the text the guest GETs return, and the signature stores the same
text and version, so what was agreed to is what was shown.

Change the wording → bump VERSION. A page holding an older version is
refused, so nobody signs against words we no longer show.

Pending counsel's review (legal doc, e-signatures question 2)."""

VERSION = "2026-10-v1"

TEXT = (
    "I agree to sign this agreement electronically and to receive it and related records "
    "electronically. My typed name is my signature and has the same effect as a handwritten one. "
    "To view and keep these records I need a web browser, an email account, and a way to open PDF "
    "files. I can ask for a free paper copy, or withdraw this consent for future agreements, by "
    "emailing jornaevents@gmail.com; withdrawing doesn't undo anything I've already signed. I'll "
    "keep my email address up to date with my vendor."
)


def view() -> dict:
    return {"version": VERSION, "text": TEXT}
