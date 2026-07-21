"""Owner-scope classification for RomM collections.

The pure predicate behind the QAM "Own / All" collection filter. RomM's
collection listings return the signed-in user's own collections plus every
other user's PUBLIC collection; "Own" hides the foreign ones. This module owns
the single rule that decides whether a collection counts as the user's own —
used both to tag the frontend list (``is_own``) and to drop foreign units from
the sync work queue. No I/O, no state: identity and the collection's owner id
come in as arguments.

Two invariants make the filter safe and non-breaking:

* **Virtual collections have no owner.** They are global/derived — RomM's
  ``VirtualCollection`` model carries no ``user_id`` column and returns them
  identically to every user — so they are always own and always survive an
  "Own" filter.
* **Unknown identity degrades to "All".** When the plugin does not yet know its
  own user id (never fetched / offline), every collection is treated as own, so
  "Own" filters nothing rather than filtering against the wrong identity.
"""

from __future__ import annotations


def is_own_collection(collection_user_id: object, own_user_id: int | None, *, kind: str) -> bool:
    """Whether a collection is the signed-in user's own (survives an "Own" filter).

    Parameters
    ----------
    collection_user_id:
        The collection's owner id (``user_id``) from the RomM listing dict, or
        ``None``/absent. Compared by value against *own_user_id*. Only user and
        smart collections carry it; virtual collections never do.
    own_user_id:
        The signed-in user's own id (``settings["romm_user_id"]``), or ``None``
        when identity is not yet known.
    kind:
        ``"user"``, ``"smart"`` or ``"virtual"``.

    Returns ``True`` (own) when the collection is a virtual collection (no
    owner), when our own identity is unknown (the non-breaking fallback), or
    when the collection's owner id equals ours. ``False`` (foreign) only when a
    user/smart collection is owned by a different known id.
    """
    if kind == "virtual":
        return True
    if own_user_id is None:
        return True
    return collection_user_id == own_user_id
