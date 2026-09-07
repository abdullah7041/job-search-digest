# Plan 016: Cap the bytes a third-party page may spend on the daily run

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 23de025..HEAD -- digest.py jd_fetch.py test_digest.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `23de025`, 2026-08-30

## Why this matters

Every HTTP fetch in the daily pipeline reads the full response body into memory
with no upper bound. Two of those fetches go to URLs the pipeline does not
choose: `rescue_apply_link` (`digest.py:1053`) issues one GET per fresh
aggregator posting, and `verify_link` (`digest.py:1199`) fetches every
candidate posting URL before delivery. Those URLs arrive from JSearch — a
third-party aggregator relaying links from LinkedIn, Indeed, Glassdoor,
Jooble and anywhere else Google for Jobs indexes. The page on the other end is
not the project's, and the 30-second `requests` timeout is a per-read timeout,
not a total-transfer budget: a body that keeps arriving keeps being read.

The concrete cost is a lost digest day. A single oversized or slow-drip
response inflates the runner's memory or holds the `execute` job until the
10-minute `timeout-minutes` kills it. The failure is loud — `notify-failure`
fires and the watchdog alerts — but the day's postings are gone, and the run
has to be re-triggered by hand.

A cap is the cheap structural answer. Job descriptions are truncated to 6000
characters (`JD_MAX_CHARS`) and ATS JSON payloads are small, so a few megabytes
is far above anything the pipeline legitimately needs and far below anything
that hurts.

This is defensive hardening on an input boundary, not a response to a known
incident.

## Current state

### The two unbounded fetchers

`digest.py:338-339`:

```python
def _get(url):
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
```

`jd_fetch.py:34-40`:

```python
USER_AGENT = "job-digest/1.0 (personal job search)"
FETCH_TIMEOUT = 30
JD_LIMIT = 6000


def default_get(url):
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
```

`digest.py` already imports from `jd_fetch` (see the import block near the top
of `digest.py`) and passes `get=_get` into `jd_fetch.extract_posting`
(`digest.py:1003`), so the two are already coupled and a shared helper in
`jd_fetch` is the natural home.

### Everything a response is used for — the complete list

This is the full contract the replacement must satisfy. It was compiled by
reading every call site; do not assume anything beyond it, and do not remove
anything from it.

| Attribute | Used at |
|---|---|
| `.status_code` | `digest.py` 360, 391, 476, 605, 961, 968, 1052, 1201–1207, 1689, 1694, 1699, 1709; `jd_fetch.py` 170, 260 |
| `.json()` | `digest.py` 362, 393, 478, 607, 962, 969, 1690, 1695, 1700, 1710; `jd_fetch.py` 173 |
| `.text` | `digest.py` 1053, 1226; `jd_fetch.py` 261 |
| `.url` | `digest.py` 1214 (`final = (r.url or "").lower()`) |
| `.history` | `digest.py` 1218 (`if r.history and (...)`) |

`verify_link` is the demanding caller — it is the only one that reads `.url`
and `.history`, and it reads `.text` as well:

```python
    final = (r.url or "").lower()
    job_tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
    if r.history and ("error=true" in final or "not_found=true" in final
                      or (job_tail and job_tail not in final)):
        log(f"DROP redirected off posting -> {r.url}: {url}")
        return LINK_CLOSED
    body = (r.text or "").lower()
```

### How exceptions are handled today

Callers catch `requests.RequestException` (network) and `ValueError` (bad
JSON). `requests.Response.json()` raises `requests.exceptions.JSONDecodeError`,
which subclasses `ValueError`. **The replacement's `json()` must also raise a
`ValueError` subclass** — `json.loads` does, so using `json.loads` directly is
correct and requires no caller changes.

### How tests inject responses

`test_digest.py` and `test_interactive.py` replace `_get` / the injected `get`
with `unittest.mock.Mock` objects carrying `status_code`, `text`, `json` and
sometimes `url`/`history`. Example, `test_digest.py:777-780`:

```python
    def _page(self, body, status=200):
        response = mock.Mock()
        response.status_code = status
```

Those tests patch the *function*, so they never construct the new wrapper.
They must keep passing untouched.

### Repo conventions

- Three runtime dependencies only: `requests`, `pypdf`, `reportlab`. Add none.
- Comments explain *why* and name the concrete failure they prevent.
- **Fail-soft**: a failure costs the user the feature, never the run. An
  oversized page must degrade to "this page gave up no description" — the same
  outcome as a 403 — never to an exception that ends the digest.
- Tests are `unittest`, offline.

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
- `jd_fetch.py` — the new cap constant, the wrapper class, and `default_get`
- `digest.py` — `_get` only
- `test_digest.py` — new tests

**Out of scope** (do NOT touch):
- `fetch_jsearch` (`digest.py:513-594`). It calls `requests.get` directly
  against a keyed first-party API with a bounded JSON body. Leaving it alone
  keeps this change to the untrusted-URL boundary.
- `_post` (`digest.py:344-351`) — Workable's POST-only board API, first-party.
- `post_discord` (`digest.py:1366`) and `discord_delivery.py` — outbound calls
  to Discord and the Worker, not third-party page reads.
- `openrouter.py` — model responses, bounded by the provider.
- Any caller's exception handling. The whole point is that the contract holds
  and no caller changes.

## Git workflow

- Branch: `advisor/016-cap-page-bytes`
- Conventional Commits. Recent examples from `git log`:
  `fix: say when a relister blocks the apply-link lookup`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baseline

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`. Record `Ran N tests` as **N_before**.

### Step 2: Add the cap and the bounded response in `jd_fetch.py`

Add, near the existing `FETCH_TIMEOUT` constant:

```python
# A page this project fetches is either an ATS JSON payload or a posting page
# whose description is truncated to JD_LIMIT anyway, so nothing legitimate comes
# close to this. The cap exists because the URLs come from an aggregator relaying
# third-party sites: requests' timeout is per read, not per transfer, so an
# oversized or slow-drip body is read until the 10-minute workflow timeout kills
# the whole digest. Four megabytes is far above any real posting page.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
```

Then add a wrapper class and a bounded fetch. Write it so a caller cannot tell
the difference for any response under the cap:

```python
class BoundedResponse:
    """A response whose body was read only up to MAX_RESPONSE_BYTES.

    Exposes exactly what this project's callers use -- status_code, text,
    json(), url and history -- so nothing downstream changes. A truncated body
    is not an error: a posting page cut short simply fails the completeness gate
    the same way a bot-blocked one does, which is the fail-soft outcome the rest
    of the pipeline is built around.
    """

    def __init__(self, response, content: bytes, truncated: bool):
        self.status_code = response.status_code
        self.url = response.url
        self.history = response.history
        self.headers = response.headers
        self.encoding = response.encoding
        self.content = content
        self.truncated = truncated

    @property
    def text(self) -> str:
        # requests would sniff an undeclared charset here; without the whole body
        # that sniffing is unreliable, so an undeclared page is read as UTF-8 and
        # undecodable bytes become replacement characters rather than an
        # exception. A page whose text is mangled fails the completeness gate.
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self):
        # json.loads raises ValueError on bad input, which is what every caller
        # already catches -- requests' own JSONDecodeError is a ValueError too.
        return json.loads(self.text)


def bounded_get(url, *, timeout: int = FETCH_TIMEOUT,
                limit: int = MAX_RESPONSE_BYTES) -> BoundedResponse:
    """GET a URL, reading at most ``limit`` bytes of its body."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=timeout, stream=True)
    try:
        chunks, total = [], 0
        for chunk in response.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        body = b"".join(chunks)
    finally:
        response.close()
    return BoundedResponse(response, body[:limit], total >= limit)
```

`jd_fetch.py` already imports `json` and `requests` at the top, so no new
imports are needed. Confirm that before adding any.

Then rewrite `default_get` to delegate:

```python
def default_get(url):
    return bounded_get(url)
```

**Verify**:
`python -c "import jd_fetch; print(jd_fetch.MAX_RESPONSE_BYTES)"` → `4194304`.
`grep -c "requests.get" jd_fetch.py` → `1` (only inside `bounded_get`).

### Step 3: Point `digest._get` at the shared helper

In `digest.py`, replace:

```python
def _get(url):
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
```

with a delegation to `jd_fetch.bounded_get`, keeping the 30-second timeout the
function has always used, and a one-line comment saying why the body is capped
(point at `rescue_apply_link` and `verify_link` as the callers that fetch URLs
the pipeline did not choose).

Add `bounded_get` to the existing `from jd_fetch import ...` line in
`digest.py`. Find that line first (`grep -n "from jd_fetch import" digest.py`)
and extend it rather than adding a second import.

**Verify**:
`grep -n "requests.get" digest.py` → exactly one match, inside `fetch_jsearch`
(around line 545). If there are two, `_get` was not converted.
`python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`, `Ran N_before tests`. **Every existing test must pass with no
modification.** They patch the fetch functions, so a green suite here is the
evidence that no caller contract moved.

### Step 4: Add tests for the cap

Add a new test class to `test_digest.py` — put it next to `TestJdFetch`
(line 774). Do not use the network: build a fake `requests.Response`-shaped
object and patch `jd_fetch.requests.get`.

Write four tests:

1. `test_a_normal_page_is_returned_whole` — a small body comes back with
   `truncated` false, `.text` equal to the original string, and `.status_code`
   preserved.
2. `test_an_oversized_page_is_truncated_rather_than_read_whole` — an iterator
   yielding more than the limit returns exactly `limit` bytes and
   `truncated` true. Pass a small `limit` (e.g. 1024) rather than generating
   four megabytes.
3. `test_a_json_body_still_parses` — `.json()` returns the parsed dict, and a
   malformed body raises `ValueError`.
4. `test_url_and_history_survive_for_verify_link` — `.url` and `.history` are
   carried through from the underlying response, since `verify_link` reads both
   to detect a posting redirected off its own page.

For the fake, a `unittest.mock.Mock` with `status_code`, `url`, `history`,
`headers`, `encoding`, `close` and an `iter_content` that returns a generator is
enough. Model the mocking style on the existing `TestJdFetch._page` helper
(`test_digest.py:777`).

**Verify**: `python -m unittest test_digest.py` → `OK`, four more tests than
step 1's `test_digest` contribution.

### Step 5: Confirm truncation degrades, never raises

Assert the fail-soft property end to end: a truncated page reaching
`jd_fetch.extract_posting` must return `{"text": "", ...}` (or a complete
description if the truncated portion happened to contain one) — never raise.

Add one test: `test_a_truncated_page_gives_up_a_description_or_nothing_but_never_raises`,
calling `extract_posting` with an injected `get` returning a `BoundedResponse`
built over a truncated HTML fragment.

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`, `Ran (N_before + 5) tests`.

## Test plan

- **New tests**: the five named in steps 4 and 5, in `test_digest.py`.
- **Structural pattern**: `class TestJdFetch` (`test_digest.py:774`) and its
  `_page` helper.
- **The load-bearing regression check**: every pre-existing test passing with
  **no edits**. If any existing test needs changing to accommodate this, the
  wrapper has broken a contract — that is a STOP condition, not something to
  fix by editing the test.
- **Verification**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
  → `OK`, exactly five new tests, zero modified ones.

## Done criteria

ALL must hold:

- [ ] `grep -c "requests.get" digest.py` returns `1`
- [ ] `grep -c "requests.get" jd_fetch.py` returns `1`
- [ ] `grep -c "MAX_RESPONSE_BYTES" jd_fetch.py` returns `2` or more (definition and use)
- [ ] `python -c "import jd_fetch; r=jd_fetch.BoundedResponse; print(all(hasattr(r,a) for a in ('text','json')))"` prints `True`
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
      → `OK`, `Ran (N_before + 5) tests`
- [ ] `git diff --stat` shows **zero** deletions in pre-existing test bodies
      (new tests added only)
- [ ] `cd worker && npm test` → `fail 0` (unchanged)
- [ ] `git status --porcelain` lists only `digest.py`, `jd_fetch.py`, `test_digest.py`
- [ ] `plans/README.md` status row for 016 updated

## STOP conditions

Stop and report back (do not improvise) if:

- Any pre-existing test fails or needs editing. The wrapper must be
  indistinguishable from a `requests.Response` for every caller in the table
  above; a failing existing test means it is not, and the right move is to
  report which attribute is missing, not to change the test.
- You find a caller using a response attribute not in the "Everything a
  response is used for" table (for example `.raise_for_status()`, `.cookies`,
  `.elapsed`, or iteration). Report the call site; the table was compiled by
  reading the code and an omission means the contract is wider than assumed.
- Decoding behavior turns out to matter for a real archived page — for example
  an existing test asserting on Arabic text starts producing replacement
  characters. Report it rather than switching to `apparent_encoding`, which
  needs the whole body and would undo the cap.
- You are tempted to set `response._content` on a real `requests.Response`
  instead of using the wrapper. Do not — it is a private attribute and its
  behavior is not part of the library's contract.

## Maintenance notes

- **What interacts with this**: `verify_link` is the most sensitive caller. It
  decides whether a job is dropped as closed based on `.history`, `.url` and
  substring checks against `.text`. If the cap ever truncates *before* a closed
  marker appears in the body, a dead posting could be delivered as open. At
  four megabytes against pages whose descriptions are capped at 6000
  characters, that is not a live risk — but if the cap is ever lowered, re-read
  `CLOSED_MARKERS` handling first.
- **The `truncated` flag is currently informational.** Nothing reads it. It is
  exposed so a future change can log or count truncations without touching the
  fetch path. If you want that logging now, it belongs in `rescue_apply_link`
  and `extract_posting` alongside their existing `WARN` lines — but it is
  deliberately out of scope here to keep the diff to the fetch boundary.
- **What a reviewer should scrutinize**: that `response.close()` runs on every
  path (the `try/finally`), since `stream=True` holds the connection open until
  the body is consumed or the response is closed. A leaked connection per
  aggregator job would be a new failure mode introduced by the fix.
- **Not done here**: no restriction on redirect targets or hosts. `requests`
  follows redirects by default and this plan does not change that. A separate
  question worth its own decision is whether a posting URL should be allowed to
  redirect to a private address at all; the runner is ephemeral and reads
  nothing sensitive, so it was judged out of scope rather than overlooked.
