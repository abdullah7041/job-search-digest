# Plan 015: Sign every Worker request through the one shared client

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 23de025..HEAD -- discord_delivery.py worker_client.py test_interactive.py worker/src/index.js`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: MED
- **Depends on**: none
- **Category**: tech-debt
- **Planned at**: commit `23de025`, 2026-08-30

## Why this matters

The HMAC scheme that authenticates every Python→Worker call is implemented
twice: once in `worker_client.signed_request` and once, by hand, inside
`discord_delivery.post_interactive_cards`. `worker_client.py` was created
specifically to end that duplication (`git log`:
`1ae265e refactor: share signed Worker request client`) — but the older caller
was never migrated, so the repository still has two copies of a security
primitive that must agree byte-for-byte with `internalRequestIsValid` in
`worker/src/index.js`.

That is not a style complaint. The two copies **already differ in one way that
matters**: the shared client derives the signed path from the URL, and the
hand-rolled copy hardcodes it.

```python
# worker_client.py:19        path = urlsplit(url).path
# discord_delivery.py:41     path = "/internal/digest"
```

The Worker signs over `new URL(request.url).pathname`
(`worker/src/index.js:165`). So the moment `INTERACTIVE_WORKER_URL` is anything
other than a bare origin — a Worker mounted under a route prefix, a custom
domain with a base path, a proxy that prepends one — the daily digest signs
`/internal/digest` while the Worker verifies `/<prefix>/internal/digest`, and
**every card delivery fails with 401 for a reason nothing in the logs
explains.** The shared client does not have that failure mode.

After this plan there is one implementation of the signing scheme, the daily
digest inherits the path-correct behavior, and any future change to the
canonical form is a one-file change instead of a two-file change where forgetting
the second file breaks production silently.

## Current state

### The two implementations

`worker_client.py:15-38`, complete:

```python
def signed_request(method: str, url: str, secret: str, body: bytes = b"") -> requests.Response:
    """Send one HMAC-authenticated request accepted by the Worker."""
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    path = urlsplit(url).path
    canonical = f"{timestamp}\n{nonce}\n{method}\n{path}\n".encode("utf-8") + body
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()
    ).decode("ascii")
    response = requests.request(
        method,
        url,
        data=body if method != "GET" else None,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Job-Digest-Signature": signature,
            "X-Job-Digest-Timestamp": timestamp,
            "X-Job-Digest-Nonce": nonce,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response
```

`discord_delivery.py:33-58`, complete:

```python
def post_interactive_cards(
    worker_url: str,
    secret: str,
    snapshots: list[dict],
    *,
    heartbeat: dict | None = None,
) -> dict:
    payload = json.dumps(interactive_cards_payload(snapshots, heartbeat), ensure_ascii=False).encode("utf-8")
    path = "/internal/digest"
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    canonical = f"{timestamp}\n{nonce}\nPOST\n{path}\n".encode("utf-8") + payload
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()).decode("ascii")
    response = requests.post(
        worker_url.rstrip("/") + path,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Job-Digest-Signature": signature,
            "X-Job-Digest-Timestamp": timestamp,
            "X-Job-Digest-Nonce": nonce,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()
```

The canonical string, the HMAC-SHA256, the base64 encoding, the three header
names, the 30-second timeout and the `raise_for_status()` are identical. The
differences are: the derived-vs-hardcoded path, an extra `Accept` header in the
shared client, and `requests.request(method, ...)` vs `requests.post(...)`.

### The verifier both must match (`worker/src/index.js:157-172`)

```javascript
async function internalRequestIsValid(request, raw, env) {
  const supplied = request.headers.get("X-Job-Digest-Signature") || "";
  const timestamp = request.headers.get("X-Job-Digest-Timestamp") || "";
  const nonce = request.headers.get("X-Job-Digest-Nonce") || "";
  if (!/^\d+$/.test(timestamp) || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  if (!/^[a-zA-Z0-9-]{16,64}$/.test(nonce)) return false;
  const path = new URL(request.url).pathname;
  const canonical = `${timestamp}\n${nonce}\n${request.method}\n${path}\n${raw}`;
  ...
}
```

Note the nonce constraint: `/^[a-zA-Z0-9-]{16,64}$/`. Both Python copies use
`str(uuid.uuid4())`, which is 36 characters of hex and hyphens — inside the
range. The shared client keeps that, so nothing changes there.

### The existing test that already pins the shared client (`test_interactive.py:312-352`)

```python
    def test_signed_request_uses_the_existing_hmac_canonical_form(self):
        ...
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                timestamp = self.headers.get("X-Job-Digest-Timestamp", "")
                nonce = self.headers.get("X-Job-Digest-Nonce", "")
                canonical = f"{timestamp}\n{nonce}\nGET\n{self.path}\n".encode()
                expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
                received["signed"] = hmac.compare_digest(
                    self.headers.get("X-Job-Digest-Signature", ""), expected
                )
                ...
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
```

That local-`ThreadingHTTPServer` pattern is exactly what the new POST test in
step 3 should copy. It is real HTTP against localhost — no network egress, no
mocking of `requests`.

### How `post_interactive_cards` is used

- `digest.py` imports it and calls it in `main()` (delivery) and in
  `post_heartbeat`. Both call sites pass `(worker_url, worker_secret,
  snapshots)` positionally, and `post_heartbeat` adds `heartbeat=` as a keyword.
- `test_digest.py` patches it wholesale (`mock.patch.object(digest,
  "post_interactive_cards", ...)`) at lines 1213, 1241, 1267 and 1297, so its
  internals are not exercised by those tests. **The signature and return type
  must not change**, or all four break.
- `test_interactive.py:27` imports `interactive_cards_payload` (the payload
  builder, not the sender) — leave that function untouched.

### Repo conventions

- Three runtime dependencies only: `requests`, `pypdf`, `reportlab`. Add none.
- Comments explain *why* and name the failure they prevent.
- Tests are `unittest`, offline; a localhost `ThreadingHTTPServer` is the
  established way to test a signed request end to end.

## Commands you will need

| Purpose      | Command                                                                       | Expected on success |
|--------------|-------------------------------------------------------------------------------|---------------------|
| Install      | `python -m pip install --require-hashes -r requirements.txt`                   | exit 0              |
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` | `OK`                |
| Worker tests | `cd worker && npm test`                                                       | `fail 0`            |

**Record your observed baseline in step 1.** At `23de025` the Python suite
reported `Ran 199 tests`.

## Scope

**In scope** (the only files you may modify):
- `discord_delivery.py`
- `test_interactive.py` — one new test

**Out of scope** (do NOT touch):
- `worker_client.py`. It is the target of the migration and is already pinned
  by an existing test. Do not "improve" it while migrating a caller onto it —
  that would change the thing you are validating against.
- `worker/src/index.js`. The verifier does not change. If you find yourself
  wanting to change it, the migration has altered the canonical form, which is
  a STOP condition.
- `digest.py`. `post_interactive_cards` keeps its exact signature and return
  type, so no call site moves.
- `interactive_cards_payload` in `discord_delivery.py` — the payload builder is
  correct and separately tested.

## Git workflow

- Branch: `advisor/015-share-signed-request`
- Conventional Commits. A recent example from `git log`:
  `refactor: share signed Worker request client`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baseline

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`. Record `Ran N tests` as **N_before**.

### Step 2: Route `post_interactive_cards` through `signed_request`

Rewrite the body of `post_interactive_cards` in `discord_delivery.py` to build
the payload as it does now and hand the signing and sending to
`worker_client.signed_request`. Keep the function's name, parameters,
keyword-only `heartbeat`, and `dict` return type exactly as they are.

Target shape:

```python
def post_interactive_cards(
    worker_url: str,
    secret: str,
    snapshots: list[dict],
    *,
    heartbeat: dict | None = None,
) -> dict:
    """Hand the day's cards to the Worker over the signed internal contract.

    The signing lives in worker_client, not here. This function used to
    hand-roll the same HMAC and sign a hardcoded "/internal/digest", while the
    Worker verifies over the request's real pathname -- so a WORKER_URL with any
    base path would have signed one string and been checked against another, and
    every card delivery would have failed 401 with nothing in the logs saying
    why. Deriving the path from the URL is worker_client's job.
    """
    payload = json.dumps(interactive_cards_payload(snapshots, heartbeat),
                         ensure_ascii=False).encode("utf-8")
    url = worker_url.rstrip("/") + "/internal/digest"
    return signed_request("POST", url, secret, payload).json()
```

Then fix the imports at the top of `discord_delivery.py`. After the rewrite,
`base64`, `hashlib`, `hmac`, `time` and `uuid` are all unused; `json` and
`requests` — check each before removing. `json` is still used by
`json.dumps`. `requests` is no longer referenced. Remove exactly the imports
that are now unused and add:

```python
from worker_client import signed_request
```

**Verify**:
`python -c "import discord_delivery"` → exits 0, no output.
`grep -cE "^import (base64|hashlib|hmac|time|uuid)$|^import requests$" discord_delivery.py` → `0`.
`grep -c "hmac.new" discord_delivery.py` → `0`.
`python -m unittest test_digest.py` → `OK`, count unchanged from step 1's
`test_digest` contribution (those tests patch the function out, so they prove
the signature did not move).

### Step 3: Test the POST path end to end against a local server

Add one test to `test_interactive.py`, immediately after
`test_signed_request_uses_the_existing_hmac_canonical_form`. Copy that test's
`ThreadingHTTPServer` harness and change it to a `do_POST` handler.

Name it:

```python
    def test_the_daily_card_delivery_signs_the_path_it_actually_posts_to(self):
```

The handler must:

1. Read `Content-Length` bytes from `self.rfile` as the raw body.
2. Recompute `canonical = f"{timestamp}\n{nonce}\nPOST\n{self.path}\n".encode() + raw`
   — note `self.path`, taken from the request, **not** a hardcoded
   `/internal/digest`. That is the whole point of the test.
3. Record whether the supplied signature matches, using `hmac.compare_digest`.
4. Respond `200` with a JSON body such as `{"posted": ["k1"], "existing": [], "failed": []}`.

The test body must:

1. Start the server on `127.0.0.1:0`.
2. Call `post_interactive_cards(f"http://127.0.0.1:{port}", "callback-secret", [<one snapshot dict>])`.
   Build the snapshot with whatever helper `test_interactive.py` already uses
   for `interactive_cards_payload` tests (see around line 118) rather than
   inventing a new fixture.
3. Assert the returned dict equals the JSON the handler sent — proving the
   `.json()` return contract survived.
4. Assert the signature verified.

**Verify**: `python -m unittest test_interactive.TestWorkerClient` (or whichever
class you added it to) → `OK`.

### Step 4: Confirm the canonical form did not move

This is the safety check that matters most: the Python side must still produce
what the Worker verifies.

Run the Worker suite, which contains signed-internal-request tests
(`worker/test/interaction.test.js` builds signed requests with
`signedInternalRequest`).

**Verify**:
`cd worker && npm test` → `fail 0`, `pass` unchanged from baseline.
`python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`, `Ran (N_before + 1) tests`.

## Test plan

- **New test**: `test_the_daily_card_delivery_signs_the_path_it_actually_posts_to`
  in `test_interactive.py`. Covers: the POST is signed over the real request
  path, the body is signed verbatim, and the return value is still the parsed
  JSON dict.
- **Structural pattern**: `test_signed_request_uses_the_existing_hmac_canonical_form`
  (`test_interactive.py:312`).
- **Existing tests that must keep passing unchanged**: the four `test_digest.py`
  delivery tests that patch `post_interactive_cards` (lines 1213, 1241, 1267,
  1297), and every test in `worker/test/interaction.test.js` that constructs a
  signed internal request.
- **Verification**: both suites green, exactly one new Python test.

## Done criteria

ALL must hold:

- [ ] `grep -c "hmac" discord_delivery.py` returns `0`
- [ ] `grep -c "from worker_client import signed_request" discord_delivery.py` returns `1`
- [ ] `grep -c "def signed_request" worker_client.py` returns `1` and
      `git diff --name-only | grep -c worker_client.py` returns `0`
- [ ] `python -c "import inspect, discord_delivery; print(str(inspect.signature(discord_delivery.post_interactive_cards)))"`
      prints a signature containing `worker_url`, `secret`, `snapshots`, `*` and `heartbeat`
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
      → `OK`, `Ran (N_before + 1) tests`
- [ ] `cd worker && npm test` → `fail 0`
- [ ] `git status --porcelain` lists only `discord_delivery.py` and `test_interactive.py`
- [ ] `plans/README.md` status row for 015 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The two functions quoted in "Current state" do not match the live code.
- Any `test_digest.py` delivery test fails. That means the public signature or
  return type of `post_interactive_cards` moved, which this plan forbids.
- Any `worker/test/interaction.test.js` test fails. That means the canonical
  string changed, which is the one outcome this migration must not produce —
  report it rather than adjusting the Worker to match.
- You conclude that `worker_client.signed_request` needs a change to serve this
  caller (for example a different header set, or a `heartbeat` special case).
  Report what is missing instead of editing it; the shared client is the
  reference implementation and changing it while migrating onto it defeats the
  check.
- Removing an import breaks something you did not expect — run
  `python -c "import discord_delivery, digest"` after each removal rather than
  removing them all at once.

## Maintenance notes

- **No Cloudflare deploy is needed for this plan.** Only `discord_delivery.py`
  and a test change; `worker/src/index.js` is untouched, and the canonical form
  the deployed Worker verifies is unchanged — which is exactly what step 4
  checks. This matters because the Worker does *not* ship by merging (that
  needs `cd worker && npx wrangler deploy`), so a plan that quietly depended on
  a deploy would sit half-live. This one does not.
- **What interacts with this**: the canonical string is defined in three places
  that must agree — `worker_client.signed_request` (Python signer),
  `internalRequestIsValid` in `worker/src/index.js` (Worker verifier), and the
  handler fixtures in the tests. After this plan there is exactly one signer.
  Any future change to the canonical form is a coordinated Python + Worker +
  redeploy change, and the Worker must be deployed before or with the Python
  side, never after.
- **What a reviewer should scrutinize**: that the body bytes are signed
  verbatim. `interactive_cards_payload` is serialized once, with
  `ensure_ascii=False`, and those exact bytes are both signed and sent. A
  re-serialization anywhere between signing and sending would produce a valid
  request with an invalid signature.
- **A behavior change worth stating**: after this plan the daily digest sends
  an `Accept: application/json` header it did not send before, because the
  shared client sets it. The Worker ignores it.
- **Deferred**: `worker/src/index.js` still verifies over the path only, not the
  query string. No internal route reads query parameters today
  (`worker/src/index.js:543-606` matches on `pathname` alone), so this is
  currently sound — but a future internal route that takes a query parameter
  would be unauthenticated in that parameter. Worth a comment on
  `internalRequestIsValid` if such a route is ever added.
