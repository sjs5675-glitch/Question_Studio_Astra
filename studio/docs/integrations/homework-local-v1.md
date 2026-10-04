# Astra same-PC homework synchronization v1

Version: `astra-homework-local.v1`
Implementation: `studio/app/homework_local.py`, dispatched by `studio/app/server.py`.
This namespace is separate from `homework-read-v1/CONTRACT.md`; that proposed
credential-based DTO contract has not been silently replaced or deployed.

## Connection

Backend-to-backend only, using the current Astra owner/listener address
(current implementation target: `http://127.0.0.1:8876`).
Every request must include the **nonsecret** header:

`X-Astra-Local-Client: homework`

No operator token, cookie, runtime receipt, or separate credential is needed.
This is intentionally a personal same-PC access policy, not process isolation.
Any same-PC process can use this header. Actual socket peer must be loopback,
Host must be the local Astra host/port, Origin and Sec-Fetch-Site must be absent.
Browser access, Tailscale/proxy exposure and all non-GET methods are denied.
The header does not authorize existing operator APIs. Existing authentication
and Host/Origin checks are unchanged; no new CORS permission is introduced.

## GET routes

Prefix: `/api/integrations/homework/local/v1`

- `/items` (no query)
- `/projects/{project_id}/items/{item_id}?revision=N`
- `/projects/{project_id}/items/{item_id}/assets/{asset_id}?revision=N&sha256=H`

IDs follow canonical `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` rules. Revision is a
positive decimal integer, no leading zero, maximum 16 digits. SHA is lowercase
64-character hex. Duplicate, missing and unknown preconditions are rejected.
There is no historical revision lookup or write endpoint.

### List

`{contract_version, source_id, complete, generation, items, unavailable}`

- `source_id`: stable nonsecret UUID derived from normalized workspace identity.
- `generation`: SHA-256 of the current sorted list and unavailable references.
- `items`: current active summaries. Each includes `project_id`, `item_id`,
  `ref`, `revision`, `updated_at`, `identity`, `tags`, `dependency`, `preview`,
  `status`, `source`, `package_sha256`, and `change_token` (digest of these
  exact fields, including the full canonical package hash). The package hash
  detects answer/explanation/review/registered-asset metadata changes even when
  the numeric item revision stays unchanged. Raw asset corruption is checked
  during asset collection, not by metadata polling.
- `unavailable`: references omitted by the bank scanner because they are
  unreadable/missing/invalid. `complete=false` when this is nonempty.
- `status.solution_link=not_checked` is deliberately not a readiness decision.

This lists **all current saved active questions**, including source-only and
unapproved questions, not two fixed samples and not just issuable questions.
Approval is not required for synchronization. Issuance and grading decisions
must remain separate. Trash/restore changes membership even at the same item
revision. Never interpret `complete=false` or a failed poll as confirmed deletion.
Absence removes an item from new selection, not from issued homework history.
An unfinished purge journal makes list polling unavailable (503), not a complete
snapshot or confirmed removal. Detail/assets for its target are likewise 503
until the existing purge operation is resolved; this API never resumes it.

### Detail

`{contract_version, source_id, package, revision, package_sha256,
  approval_valid, registration, solution_link, asset_urls}`

`package` is the stored canonical question package, preserving nested content,
choice IDs, assets, provenance and review evidence. It can contain local source
paths/reviewer data; retain it on the homework backend, do not dump the complete
package into public browser diagnostics. Jobs, OCR candidates, credentials,
operator configuration and source PDFs are not included by this endpoint.
`approval_valid` is computed on Astra from current canonical revision/digest and
existing file-inclusive package/review validation. False does **not** prohibit
catalog synchronization; it prevents pretending an unreviewed item is approved.
`registration.usable` alone is not an issuance or automatic-grading approval.
`package_sha256` is SHA-256 of deterministic full-package JSON serialization
(ensure_ascii=False, sort_keys=True, compact separators, no NaN), not an approval
digest and not a signature. `asset_urls` uses the local revision/SHA-bound routes.

### Assets

Existing ready registered PNG/JPEG/BMP files only. No source PDFs, generated
solution-page GETs, rendering, OCR or model calls. Reads remain in the item's
resolved directory; absolute paths/traversal are rejected. Up to 64 MiB per
asset. The file is opened once, those bytes are verified against requested and
canonical SHA and those exact verified bytes are sent.

Response headers: Content-Type, Content-Length, Cache-Control: no-store,
X-Astra-Contract-Version, X-Astra-Revision, X-Astra-Asset-SHA256.

### JSON integrity and errors

JSON responses carry `X-Astra-Contract-Version` and `X-Astra-DTO-SHA256` of exact
UTF-8 response body bytes before JSON parsing. Server does not compress.
Clients should send `Accept-Encoding: identity`, verify raw bytes then parse.
Allow at least 16 MiB for a full bank list; no small 2 MiB transport limit.

Errors use `{contract_version,error:{code,...}}`:
- 400 `invalid_request`: invalid detail/asset preconditions
- 403 `forbidden`: wrong local client policy or non-GET method
- 404 `not_found`: unknown route, missing/deleted item or unregistered asset
- 409 `revision_conflict` with `current_revision`: collect a fresh list
- 409 `asset_sha_mismatch`: do not publish a corrupt snapshot
- 503 `source_unavailable`: keep previous cache visibly stale, retry later

An unknown/query-bearing list path is not a supported endpoint (404).
Host/Origin denial by the outer existing server can use the operator error shape.

## Automatic synchronization client

1. On startup/reconnect, GET the full list; poll regularly (recommended 2–3
   seconds for this same-PC slice; this is bounded-latency polling, not a
   guaranteed zero-latency push event stream).
2. Compare composite IDs, revision and change_token, and current membership.
   Publish the current metadata generation atomically on the homework side.
   Preserve unapproved/source-only entries as catalog entries with real status.
3. Fetch new/changed required details at list revision. Resolve every required
   asset through returned asset_urls and verify SHA of actual response bytes.
   A detail/body cache may be collected lazily on viewing or selection, but
   label it as such; do not report complete byte synchronization for metadata only.
4. Re-GET detail with the same revision after asset collection and require the
   same package_sha256/response digest before publishing the canonical snapshot.
   On 409, start again from a fresh list; retry with a bound, never fake success.
5. Keep issued homework as immutable versioned copies. Removing/holding a
   source entry affects future selection only, not previously issued answers.
6. On connection failures retain the previous cache and expose stale/error state.
   On recovery reconcile the whole current list (including new/restored items).

A collection is per-item consistent, not an atomic multi-item bank transaction;
polling catches latest saved state, not every intermediate revision event.

## Verification commands

Use the existing project venv and include project root and studio on PYTHONPATH:

`python -B -m unittest discover -s studio/tests -p test_homework_local.py -v`

Tests use anonymous temporary workspaces and no production modification/model
calls. They exercise new saved items, CAS edits, revision conflicts, raw JSON
hashes, asset hashes/corruption, local-policy denials, operator auth preservation,
trash/restore and unreadable-list completeness. Live rollout and homework UI
synchronization must be verified separately; these tests do not prove them.
