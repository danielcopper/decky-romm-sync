---
paths:
  - "py_modules/adapters/romm/**"
---

# RomM HTTP error translation `[ours]`

No mechanical check exists for the 404 rules below — only the tests named at the bottom, and they pin the call sites
that exist today, not the one you are about to add. The choke-point rule at the end is the one exception: it is checked.

## Every request goes out through `_urlopen` — checked

The `RetryLadder` (`retry.py`) each `RommHttpAdapter` holds remembers whether the RomM server is known unreachable, and
while that bit is set every retry ladder runs a single attempt with no backoff. It is cleared from exactly one place in
the transport — `_urlopen`, which calls `self._retry.note_reachable()` — so that a response arriving on **any** path
clears it, including the paths that deliberately skip `with_retry` (the reachability probe and the heartbeat both run
through `request_once`).

So a new request method must send its request with `self._urlopen(req, timeout=...)`, never by calling
`urllib.request.urlopen` itself. Reaching for `urlopen` directly fails silently in the worst way: the call succeeds, the
tests pass, and the plugin just stays in degraded single-attempt mode until some unrelated path happens to succeed.
`scripts/check_urlopen_choke_point.py` enforces this structurally (AST call sites, not dataflow — it would not see an
alias or a `getattr`).

Pass `romm_origin=False` when the request does **not** go to the configured RomM server. There is one such caller today,
`download_external`, which fetches a ROM's `url_cover` from a third-party CDN: a dead CDN must not mark RomM
unreachable, and reaching the CDN is no evidence that RomM came back. Its ladder is entered through
`self._retry.enter(..., romm_origin=False)` for the same reason.

The bit is set only where a ladder gives up, and an error carrying a **4xx status code** never sets it — the server
answered, whatever it answered. That peel is deliberate and load-bearing: `classify_error` is a user-messaging
classifier that folds every unbranched `RommApiError` onto `server_unreachable` so a display string always exists, which
would otherwise sweep in the routine 409 that every `overwrite=false` save upload is designed to provoke. A 404 that
arrives as a plain `RommApiError` carries no status code and does still set it — nothing proved RomM answered it, the
same fail-open reading described below.

`RommNotFoundError` does not mean "the server said 404". It means **RomM's entity layer said this entity does not
exist**, and downstream that reading is authority: the removed-game cleanup deletes on it, the version picker marks a
version vanished on it, save-sync drops a stale device registration on it.

A bare 404 does not carry that meaning on its own. FastAPI answers a misconfigured path prefix with the same status and
a generic `{"detail": "Not Found"}` body, and a reverse proxy in front of RomM (Cloudflare Tunnel, Traefik) answers a
misroute with an HTML or empty one. So on the **API routes** `translate_http_error` raises `RommNotFoundError` only for
a response that proves RomM answered: a JSON content type whose body parses to an object carrying a `detail` string that
is neither blank nor FastAPI's stock `Not Found`, matched case-insensitively. The requested id is deliberately **not**
parsed back out of that detail — its wording moves between RomM releases while the generic default stays put, so
blocklisting the default is the robust test.

Every other 404 shape — proxy HTML, an empty or unparseable body, a non-object body, an absent or non-string `detail`,
FastAPI's generic route-404 — degrades to a plain `RommApiError`, the transport class `classify_error` maps to
`server_unreachable`. Each caller then fails **open**: infrastructure can no longer authorize a deletion.

## The strict proof is the default; the exemption is opt-in

This polarity is the part that breaks silently. A new API call inherits the strict proof by doing nothing. Opting out is
explicit and currently has exactly three call sites, all in `adapters/romm/http.py` — `download`,
`download_conditional`, `download_external` pass `translate_http_error(..., asset_route=True)` and keep the plain status
mapping.

Those three are byte-stream fetches: their 404 answers about a **file**, not an entity, so it is never
deletion-authority grade (`download_external` reaches a third-party CDN, which has no entity layer at all). They also
have to keep raising `RommNotFoundError`, because RomM serves its cover resources from a static mount where a genuinely
missing cover answers with exactly the generic route-404 body — and the one consumer that branches on it reacts by
refetching the cover from the ROM's external `url_cover`, which is non-destructive and self-correcting.

**Never unify the two directions.** Demanding the entity proof on the asset routes disarms the cover fallback; dropping
it from the API routes hands deletion authority back to any proxy that answers a 404.

Adding a fourth byte-stream fetch is a deliberate decision, not a copy-paste: it opts out only if its 404 can never
authorize a destructive action, and each existing call site carries that constraint as a comment.

Both directions are pinned in `tests/adapters/romm/test_http.py`: `TestNotFoundDiscrimination` for the translation
itself, and a `test_generic_route_404_still_raises_not_found` in `TestRommDownloadErrors`, `TestDownloadConditional` and
`TestDownloadExternal` for each opt-out call site. Full detail, including the RomM-version caveat on the entity-answer
shape:
[what makes a 404 an entity verdict](../../docs/architecture/backend-architecture.md#rommhttpadapter-notes-what-makes-a-404-an-entity-verdict).
