# Job-search digest

A daily digest of new senior tech jobs in Saudi Arabia, posted to Discord at 07:00 AST. Same shape as your AI-news digest: gather, filter, rank, deliver, run on GitHub Actions cron.

This folder is the plan plus the working code. Build and test the live parts in Claude Code. The Cowork sandbox can't POST to Discord or reach the model APIs, so the real test runs where the network is open.

## The source decision

You asked whether LinkedIn is still the best source. It isn't, for an automated daily job.

LinkedIn Jobs and Indeed have no free API. Scraping them breaks their terms, the HTML changes often, and they block datacenter IPs like GitHub Actions runners fast. That path is fragile and you'd spend your time fixing scrapers.

The reliable source is each company's own hiring system. The companies you care about post through free, public, JSON APIs: Greenhouse, Ashby, Workable, Lever, and Pinpoint. No keys, no scraping, allowed by design. You point the script at a list of companies and pull their open roles directly. The engine reads all five, every one verified live against the real API.

There's no single board that covers Saudi tech, so the list is built company by company. That's the real work here, and why expanding it means confirming each firm's system, not guessing.

## Verified live (24 Jun 2026)

I fetched all four from the real APIs during the build:

- **HALA** — Greenhouse, token `hala`, US host. 15 roles including AI Engineer, Automation Engineer, Site Reliability Engineer, Software Backend Engineer (Riyadh).
- **Careem** — Greenhouse, token `careem`, US host. AI and founding-engineer roles, including a Founding Forward Deployed Engineer for enterprise agentic AI. Mostly UAE and Pakistan, so few Saudi roles surface, but it costs nothing to keep.
- **Tamara** — Greenhouse, token `tamara`, EU host. 30 roles including Senior Product Engineer - Backend, Product Engineer II, Senior Product Manager (Riyadh).
- **Lean Technologies** — Ashby, token `LeanTech`. Roles including Senior Solutions Engineer and Staff DevOps Engineer (Riyadh).

All four return the fields the matcher needs: title, location, apply URL, publish date.

## Coverage (23 Jul 2026)

The old candidate list is fully resolved. 14 companies now feed the pipeline: **216 postings fetched, 24 matched** — up from 127 and 10.

- **Salla** — Workable, 27 roles. The strongest single addition: 13 matches, including the highest-scoring roles in the whole digest (Senior AI Backend Engineer, Senior SRE/MLOps, Senior Data Scientist, all Saudi).
- **Tabby** — Pinpoint. `careers.tabby.ai` redirects to `tabby.pinpointhq.com`; that's why Greenhouse came up empty. 62 roles, 26 in Saudi.
- **Mozn, Unifonic, Jahez, Zid, Nana, Rasan, Sary** — real Workable accounts, but 0 open roles today. Kept in `COMPANIES` on the Careem precedent: one cheap fail-soft call a day, and a senior role surfaces the moment one lands.

### Ruled out — don't re-litigate

Big Saudi employers mostly can't be reached without scraping, which this project refuses:

- **stc** runs SuccessFactors (`sapsf.com`) — no free public JSON API.
- **NEOM** serves its own SPA with no public feed found.
- **Aramco** is not on Greenhouse. The "greenhouse" string on its careers page is *greenhouse gas* — it's an oil company. Easy false positive.
- **SmartRecruiters** is useless for discovery: its API answers `200 {"totalFound":0}` for *any* string, so it happily confirms companies that don't exist. 13 KSA employer ids were probed, all empty. No verified company means no fetcher.

Reaching these would need the JSearch aggregator (`RAPIDAPI_KEY`), which is deliberately left off.

## Adding a company

Run the discovery probe first:

```
python digest.py --discover salla tabby mozn
```

No machine with network egress handy? The **Discover company ATS boards** workflow (`.github/workflows/discover.yml`) runs the same probe from the Actions tab — `workflow_dispatch`, one `names` input, read-only, no secrets — and prints the same output to the job log. `digest.py`'s `CANDIDATES` block lists the employers the aggregator keeps surfacing that this list does not reach directly, which is what to feed it.

It probes all five ATS APIs and prints a ready-to-paste `COMPANIES` line with the live job count. It guards itself with a known-bad control token first — if an ATS answers for nonsense, its results are reported `UNRELIABLE` rather than believed. That guard is the whole point; without it SmartRecruiters-style APIs invent companies.

A miss isn't proof of absence — the probe guesses the company name as the token, so anything whose token differs (Lean is `LeanTech`) stays invisible. In that case, open the careers page and read where Apply sends you:

- `boards.greenhouse.io/<token>` or `job-boards.eu.greenhouse.io/<token>` — Greenhouse. Add `{"source":"greenhouse","token":"<token>","region":"us"|"eu"}`.
- `jobs.ashbyhq.com/<token>` — Ashby. Add `{"source":"ashby","token":"<token>"}`.
- `apply.workable.com/<token>` — Workable. Add `{"source":"workable","token":"<token>"}`.
- `jobs.lever.co/<token>` — Lever. Add `{"source":"lever","token":"<token>"}`.
- `<token>.pinpointhq.com` — Pinpoint. Add `{"source":"pinpoint","token":"<token>"}`.

Every fetch is fail-soft: a wrong token or a down API logs a WARN and is skipped, so a bad entry never breaks the run.

## How it filters

The job is matching, not relevance ranking, so the filter is strict before anything gets scored.

1. **Location** must be Saudi (Riyadh, Jeddah, Dammam, and so on). Everything else is dropped, including the Dubai and India roles these companies post.
2. **Seniority** drops junior, intern, graduate, trainee, tamheer, associate, entry — and, since 31 Jul 2026, everything above senior IC too: lead, staff, principal, head-of, director, engineering manager, team leader. The 2026-07-31 digest scored Lead/Staff roles 85–98 because seniority was only bounded from below.
2b. **Title-vs-location conflict** drops a job whose title says it's really elsewhere ("Bangkok based", "relocation provided") even when the board's location metadata claims Saudi. Same digest delivered Agoda's Bangkok role as Riyadh because only the metadata was checked.
3. **Title** must hit one of your target areas: engineer, software, AI/ML, data, DevOps/SRE, platform, automation, architect, product manager, solutions engineer.
3b. **Link quality**, in two passes, both before the freshness check so a dropped posting never costs a verification request, a model call, or a line in the digest.

   `prefer_employer_postings()` drops an aggregator copy of a posting this run already read from the employer's own board. The URL dedupe above cannot catch these, because the two links differ: Foodics' "Senior Data Scientist / Senior ML Engineer" arrived from Workable on 2026-08-11 as `apply.workable.com/foodics/j/E2707EDFCE/`, then from JSearch on 08-17, 08-18 and 08-19 as a `jobleads.com` link with a byte-identical title. Nothing joined them, so the paid copy went out three more times. Matching is on employer plus title, with the employer name reduced to its latin core first — JSearch returns `Zid | زد` and `Qiddiya | القدية` where the ATS returns `Zid` and `Qiddiya`.

   `on_paid_relister()` then drops what is left on a host that charges to reach the apply step. JobLeads is the reason: 41 of the 178 links archived over 20 days, every one costing a subscription to act on while the employer's posting was free. Jooble, Jobrapido, NaukriGulf, beBee, Wuzzuf, learn4good and TheBigJobSite are the same wrapper without the price tag. **LinkedIn, Indeed and Glassdoor are deliberately not on that list** — they are free to act on, and LinkedIn alone is 65 of those 178 links. Replayed over the archive, the two passes remove 100 of 178 links and leave every LinkedIn, Indeed and ATS one standing.

   What survives both passes still mostly links to LinkedIn, and a LinkedIn page is a login wall. So enrichment does one more thing: `jd_fetch.extract_posting()` reports the employer's own posting URL alongside the description, and `record_apply_link()` stores it as `apply_url`.

   For a posting the board shipped no description for, this is free: `fetch_posting_page()` is already hopping to the employer's ATS to read the JD, and its result was simply being thrown away. That branch is not the common case, though — JSearch always ships a description, so all 171 aggregator postings archived to 2026-08-28 returned before reaching it, including the 48 that arrived as a teaser plus a relister link. `rescue_apply_link()` covers those, and it does cost **one extra GET per aggregator job**, bounded by the fresh-job count and skipped entirely for jobs read from an employer's own board. How many succeed depends on whether the page loads for a datacenter IP and names a board, which is not knowable offline.

   The link is only believed if the board token agrees with the employer name (`employer_matches_token()`). `ats_reference()` matches any ATS-shaped href on the page, and a relister's "people also viewed" rail is full of them; nothing downstream would catch a wrong one, because `verify_link` confirms it open — it *is* a live posting — and the snapshot then hands it to a later "Tailor CV" click as the JD to re-read. A refused link costs one rescue; an accepted wrong one tailors the resume against another company's job.

   `apply_url` is recorded *alongside* `url` rather than replacing it, on purpose. `job_identity()` keys an aggregator posting on its URL, so rewriting it would make every rescued job look new on the next run and redeliver it daily for as long as the rescue kept working. The digest line, the Discord card and the snapshot all hand over `apply_url or url`; `seen.json` keeps keying on the original.

   A rescued URL is *built* from a board token and a posting id, so unlike every other link in the digest it has never been fetched when it is recorded. It is verified before being handed over, and on a definite negative the **rescue** is discarded, not the job: a 404 there is as likely to mean the URL shape was wrong as that the posting closed, and the original link was verified minutes earlier.

   A paid-relister drop is *not* written to `seen.json`. Unlike the delivery floor below, it is a verdict on the link, not on the role: the same posting appearing on the employer's own board tomorrow should be delivered tomorrow.

   Neither rescue can reach a paid relister, as of 3 Sep 2026. The relisters block this runner outright — 403 from jooble and jobrapido, 404 from jobleads — so the page hop never loads, and a live probe of JSearch's `apply_options` found exactly one entry per job, always the same publisher as the primary link, on all 20 jobs read. Eight matching postings a day were being discarded with no route to a free link at all.

   So the employer is reached instead of the link. Every posting the cut discards has its employer probed against the five supported ATSs, using its name, the first word of its name, and the registrable label of JSearch's `employer_website` as token candidates. `employer_website` is Google's knowledge-panel guess rather than a verified field — it returned `siccode.com` for "Business Codes" and `infinitiusa.com` for "Infinniti" — so a candidate is only probed once `employer_matches_token()` agrees it belongs to this employer, the same test a link read off a page has to pass. A hit is then corroborated against the board itself: Greenhouse and Workable publish the owner's name, and it has to agree with the employer; Ashby publishes none, so there the token must *be* the employer's whole key rather than a shortening of it. Without that check the first-word candidate sent `latitude` to a Greenhouse board of automotive technicians in Pittsburgh and `silver` to an Argentine recruiter on Ashby, both on 3 Sep 2026. A corroborated board is recorded in `boards.json` and read by `fetch_company()` from the next run on, at which point `prefer_employer_postings()` drops the relisted copy on its own and the JD arrives complete instead of as a teaser. Misses are recorded too, and not re-probed for 30 days: these postings are never written to `seen.json`, so the same employers return every single morning.

   The aggregator's request budget is the other tunable here. JSearch's plan allows **200 requests a month** — read off the `x-ratelimit-*` response headers on 3 Sep 2026, which is the first time anything in this project knew the number. `fetch_jsearch` reads page one of each of the three queries, then spends whatever `JSEARCH_DAILY_REQUESTS` (default 5) has left on second pages, rotating which queries get the deep read so none is permanently the shallow one. Page two is worth the request: probed the same day, all ten of its results were absent from page one. The deep read yields entirely once the reported allowance falls below `JSEARCH_QUOTA_RESERVE`, because page one is the whole aggregator input and an exhausted plan removes it until the month resets. The remaining balance is logged every run.

4. **Score at least 50.** `MIN_DELIVER_SCORE` is the line below which a job is not delivered at all. The tiers only ever decided which *heading* a job appeared under, so everything that cleared the hard filters was delivered no matter how poorly it scored, and the weak ones crowded out the review tier. Override it with a repository *variable* of the same name.

   Only a model score can send a job here. `apply_ranking` falls back to `keyword_score` whenever the ranker is missing or its answer fails validation, and those numbers are calibrated for ordering, not for a pass/fail line — dropping on them would turn a ranker outage into an empty digest with nothing anywhere saying why. A job dropped on its score is remembered like a delivered one, so tomorrow does not re-fetch, re-score and re-reject it for the next 120 days.

   The drift average is taken *before* the floor cuts. `check_drift` compares today's average against the prior window, and measuring it after a cut that removes exactly the low scores would make a stale profile look like a healthy one.

5. **Not seen before.** `seen.json` holds every posting already sent, so you only get new ones.

   What it remembers a posting *by* matters more than it sounds. JSearch mints a fresh opaque id for the same job on every request, so a state file keyed on that id never matched twice and the same role arrived day after day. Over the first 11 archived days, 65 of 122 aggregator cards were repeats, and one JAL frontend role landed on 9 separate days under 11 different ids. `job_identity()` keys an aggregator posting on its canonical URL, which does not move. LinkedIn's `refId` and `trackingId` are stripped because they change per response; Indeed's `?jk=` is kept, because it *is* the job. An ATS id is canonical already, so those postings keep the bare id every existing `seen.json` entry uses.

   `drop_repeat_descriptions()` is the second pass, after JD text is fetched and before any model call: an aggregator that relists the same employer/title under a fresh URL of its own (Jooble did it once in 11 days) is still caught, because its description is fingerprinted with normalized employer and title context. Shared boilerplate from a different role remains deliverable.

Salary is a soft signal, not a filter. These boards almost never publish a number, so a hard salary floor would reject nearly everything. The benchmark for your floor: senior engineer in Riyadh runs SAR 25,000 to 35,000 per month in 2026, AI/ML 28,000 to 35,000. The config records SAR 25,000/month for reference and never rejects a posting for missing salary.

Pinpoint is the one source that does publish structured pay (`compensation_minimum/maximum`, currency, frequency), so Tabby roles can show a real `SAR/month` range in the digest line. It stays a display signal — nothing is ever rejected on it.

One caveat the filter can't catch: some Gulf roles are nationality-gated (for example Tamara's "Builders Program - Emirati National" lines). The title doesn't always say so. You'll spot these when you read the digest.

## How it ranks

One model call scores the survivors 0 to 100 and writes a one-line "why it fits," using a short profile of you. Since 31 Jul 2026 the prompt carries an explicit rubric (90+ only for senior-IC core-area roles whose JD requirements the profile covers; leadership/staff/wrong-location scores 0–39; never above 85 without JD text). If `OPENROUTER_API_KEY` is missing or the call fails, it falls back to deterministic keyword scoring so a model outage never kills the run. Same fallback design as the news digest.

**The excerpt it scores starts at the requirements, not at the top of the posting.** It used to be the first 900 characters, which on a McKinsey posting is entirely recruiting copy — the role itself begins at character 1931. The model reported, accurately, that the description named no technical requirements; it had never been shown them. `job_archive.jd_signal` finds the first heading that introduces the work (`Your Impact`, `Responsibilities`, `Requirements`, `Qualifications`, and the rest) and takes `JD_RANK_CHARS` from there. Of the 34 distinct archived descriptions longer than 1500 characters, 10 put that heading past the old 900-character cut and 8 carry no recognised heading at all.

**The description is looked for in three places, not one.** An aggregator hands over a title and a URL, and stripping tags off the page it links to used to be the whole last-resort path — which is why so many cards said "Job description needed before tailoring". `jd_fetch.extract_jd` tries the page's `JobPosting` JSON-LD block first (a board that wants to be indexed by Google for Jobs publishes one, and it holds the description in full even when the visible page is a sign-in wall), then hops to the employer's own ATS when the aggregator is relisting a Greenhouse, Lever, Ashby, Workable or Pinpoint posting — the same APIs this project already reads for its tracked companies, reached through the same endpoints. The page body is the fallback it always was. The hop runs even when the page fetch itself failed, because an aggregator URL is often already an ATS posting. Everything returned passes the same `jd_completeness_failures` gate, so a bot-blocked page still yields nothing rather than scoring on navigation chrome.

**A teaser is scored as a teaser.** Aggregators publish a paragraph of marketing copy that clears every completeness check — 400 characters, 60 words, the word "experience" — while evidencing nothing; 23 of the first 104 captured descriptions were under 1500 characters. `job_archive.jd_grade` calls those `teaser`, the ranker is told what it is reading, `cap_teaser_scores` holds them below the strong-match line, and the card footer says so. They stay tailorable, with a warning in the message.

**An unstable ranker is visible rather than averaged away.** `score_instability` reads the committed archives and reports postings whose score moved while their JD digest did not — keyed on the description hash, because the aggregator mints a fresh id for the same posting every day. It found nine, worst at a 55-point spread. The daily average `check_drift` watches could never show that.

The digest makes that confidence visible instead of hiding borderline roles — above the delivery floor. **Strong matches** have a validated Gemini score of at least 80 and JD text. **Worth reviewing** passed every hard eligibility filter but have a lower or invalid score, no JD text, or another fit uncertainty. The floor and the tiers do different jobs: `MIN_DELIVER_SCORE` (see "How it filters") decides whether a job is delivered, `STRONG_MATCH_MIN_SCORE` decides which heading it lands under. This favors finding viable roles without presenting every result as equally ready to apply for. Every delivered job, either tier, still gets its own Tailor CV button (see below); a card without captured JD text says so in its footer, and clicking it makes the runner read the posting again rather than guessing.

`RANK_MODEL = google/gemini-3.7-flash` does the scoring, through OpenRouter, with `google/gemini-3.6-flash` and `openai/gpt-5.6-luna` behind it; both ids are overridable through repository *variables* of the same names. Read from the OpenRouter catalogue on 20 Aug 2026: $0.375/M in and $1.875/M out, carrying a `0.75` discount on its Vertex endpoints, which makes it *cheaper* than 3.6 Flash while scoring materially higher (Artificial Analysis intelligence 56 vs 51.6). This used to be `gemini-3.1-flash-lite` on the Gemini API directly, sized for classification over short records — but scoring now means reading a requirements section and weighing it against a profile, which is worth a real model. At 25 jobs a day it is still under a quarter a month, and it retires the second API key.

## Tailoring, on demand

Every card in the digest — Strong or Worth reviewing — carries a "Tailor CV" button. Its `custom_id` is `tailor:<first 32 hex chars of sha256(job_key)>`; the job key itself never appears in the id. Clicking it sends a signed Ed25519 interaction to the Cloudflare Worker, which checks the interaction's guild, channel, and user against its own configured ids before doing anything else — only the one person this deployment is scoped to can trigger a tailor.

If that check passes, the Worker looks up the immutable snapshot captured for that exact Discord message at delivery time. When that snapshot holds a captured description, the Worker re-verifies it against the SHA-256 recorded alongside it and only proceeds if it still matches; `interactive_runner.py` then re-checks the same hash and the completeness gate a second time, after fetching the task over HMAC-authenticated HTTP. An altered snapshot fails closed at both.

**A card without a captured description is no longer a dead end.** The click used to stop at the Worker with "A full job description is required; use /tailor-jd to provide it" — naming a command that was not registered, for the one case that is actually recoverable. The snapshot now reaches the runner, which resolves the description itself, worst source last:

1. the archived text, hash-verified as above;
2. text the candidate supplied, through the paste box or `/tailor-jd`;
3. the posting read again *now*, through the same `jd_fetch.extract_jd` the daily run uses — a page that blocked GitHub's runners at 07:00 often does not block them at 13:00;
4. screenshots, transcribed by a vision model (`VISION_MODEL`, defaulting to `google/gemini-3.7-flash` through the same OpenRouter transport and no-retention policy as everything else).

Every one of them passes the same completeness gate, and whichever won is named in the Discord message. A CV built from a screenshot and one built from the archived posting are not equally trustworthy, and the candidate is the one deciding whether to send it. Only if all four miss does the click report back — and it names `/tailor-jd` and the URL it could not read, so there is always a next step.

### Tailoring a job the digest never delivered

`/tailor-jd` exists for the postings this pipeline cannot reach at all — a LinkedIn role behind a login, something a friend sent you. It takes an optional `url` and an optional `image`, and needs neither:

- **`/tailor-jd title:<role> company:<employer> url:<link>`** — the runner reads the posting itself. The title and company make the target headline and message truthful.
- **`/tailor-jd title:<role> company:<employer> image:<screenshot>`** — the vision model transcribes it. Attach the screenshot to the command.
- **`/tailor-jd`** with neither — Discord opens a paste box: job title, company, an optional link, and the description in two paragraph fields. One text input caps at 4000 characters and real postings run past that, so the description arrives in two boxes and is joined back together; five rows is the modal maximum, which is exactly what this needs.

The command builds a snapshot in the same shape the daily run archives, so tailoring has one code path rather than two. What it cannot honestly fill in, it leaves empty: no score, no tier, no link check, and `source: "manual"`.

**Re-register after changing the command set.** `worker/commands.js` is the source of truth and `register-commands.mjs` PUTs exactly what is in it, so a command removed from that file is removed from Discord on the next run — and a command left in Discord without being re-registered survives with nothing behind it. That is what happened to `/tailor-jd`: it was registered against an earlier build, dropped from `commands.js` without the registration being re-run, and answered "Unsupported command." for every use while an error message elsewhere in the Worker still told you to reach for it. `worker/test/commands.test.js` now pins the registered set and checks every name has a dispatch branch, but only running `npm run register --prefix worker` changes what Discord actually shows.

### How the tailoring stays honest

`resume_render.parse_resume` splits the master into sections, entries and bullets, giving every bullet a stable ref. `resume_tailor` sends those refs with the JD and gets back the refs to keep, in order, each with its rewritten wording. Nothing without a ref can enter the document.

`resume_source.validate_tailored_resume` then checks each rewrite against the exact bullet it cites: every number must survive verbatim, and every proper noun must already exist in the master or the job title, so an invented framework or cloud platform is rejected rather than sent. A rejected draft goes back to the model once with the specific failures; if it fails again, the untouched master resume is delivered with the reason stated in the message. The same thing happens when `OPENROUTER_API_KEY` is absent — you get your master resume and a note saying why it wasn't tailored. A failure costs you the tailoring, never the document.

**What the model is asked to do with the JD changed; what it is allowed to invent did not.** Three things were shipping unchecked, and a tailored CV for a KAUST AI/ML Support Analyst role is what surfaced all three:

- **The summary was the master's summary, word for word.** Rule 5 only forbade inventing facts; nothing asked the model to aim the summary at the job, so it took the safe path on every click. It now has to open with the two or three things the job asks for that the resume genuinely evidences, in the resume's own words, and returning the existing summary unchanged is named as a failure. The three-sentence cap is enforced in code rather than requested in prose.
- **The skills block shipped identically on every job**, because `apply_plan` rewrote `section["lines"]` only under EXPERIENCE and PROJECTS — so `Frontend: React 19, Tailwind CSS, Vite, Zustand` went out on a supercomputing-lab role. Skills lines are now reordered and pruned for the job, and held to a *stricter* rule than the rest of the document: `subset_failures` requires every word of a rewritten skills line to already be in that same line. Reorder freely, drop what the job has no use for, add nothing. Resume-wide vocabulary is too loose here — it would let a term from the experience section drift into the skills list, where a recruiter reads it as a skill the candidate listed. Every category survives regardless: the model chooses which line leads, never whether a category exists.
- **The headline went into the document unchecked.** `headline_failures` holds it to the candidate's own headline plus the target job title, which is all rule 4 ever permitted.

None of this loosens rule 2. The KAUST CV carried no SLURM, Singularity or PyTorch because `resume.md` carries none, and it still won't: the job description decides what to lead with and what to drop, never what to add.

The PDF is single column, standard fonts, no tables, forced to one page by shrinking type first and dropping the least relevant bullets second. Late sections are trimmed before early ones — iterating forward took a project away before a sixth certificate, and both before a skills line the job actually asked for. Within a section the last item goes, which after `apply_plan`'s ordering is the one the job supported least. Gaps and fit notes travel in the Discord message, never in the PDF — the attachment is always safe to forward as-is.

**`LANGUAGES` was missing from every PDF this had ever rendered**, master and tailored alike. `parse_resume` files a plain line under `intro`, because only a `- ` line becomes a bullet, and the renderer skipped any section with no entries and no lines — so `Arabic (native) · English (professional)` was dropped silently, including from jobs that ask for English and for working in an international team. A section that is only an intro now renders.

The Worker posts the PDF as an ordinary message in the private channel, using the bot token. It previously attached the PDF to the interaction as an ephemeral reply (`flags: 64`); ephemeral messages are absent from channel history and vanish when the Discord client reloads, so the document disappeared and no copy survived anywhere. The channel is the one the Worker already restricts to a single guild, channel and user id, so the audience is unchanged — only the lifetime is. Gaps and fit notes still travel in the message rather than in the PDF, and nothing tailored is committed to the repository. The interaction's own message gets a short ephemeral acknowledgement, best effort: if its token has expired the channel message is already delivered.

Tailoring calls **OpenRouter**, not the Gemini API directly. It used to call `generativelanguage.googleapis.com` with a single model and no retry, so a 404 on the model name or a 429 on the quota ended the attempt outright and returned the untailored master resume. OpenRouter takes a chain — `TAILOR_MODEL` then `TAILOR_FALLBACK_MODELS`, defaulting to `openai/gpt-5.6-luna`, `google/gemini-3.6-flash`, `minimax/minimax-m3` — and walks it itself when a member is missing, rate-limited or erroring. Both are overridable through repository *variables* of the same names, which `interactive.yml` passes through; an unset variable renders as an empty string, so the code falls back with `or`, never with `os.environ.get(name, default)`.

Chain members are picked for instruction-following per token, not headline price: the evidence check rejects a draft that loosens the rules, and a rejected draft costs a whole retry, so the flash-class tier below these saves nothing. Every member must accept `response_format` and `reasoning_effort`, because one request body is sent to whichever of them serves the call. Routing sets `data_collection: deny` — the prompt carries the master resume, contact details included — and `require_parameters: true`, so no endpoint that would ignore the schema and answer in prose can serve the request. Scoring uses the same transport and key with the same no-collection policy, configured separately in `digest.py` (see "How it ranks" above).

`require_parameters: true` cuts both ways, and **no request sends `temperature`**. The flag drops any provider missing a parameter the request names, and Google serves Gemini Flash on Vertex endpoints that take `seed` but not `temperature` — which are the discounted ones. Naming `temperature` quietly routed around them to buy a knob `seed: 7` already provides here. The shared transport lives in `openrouter.py`, used by both scoring and tailoring.

One click always reports an outcome. `resume_tailor` holds a 210-second budget spanning both evidence attempts and every HTTP retry, because a workflow killed at its 10-minute timeout posts nothing at all and leaves "Preparing your tailored CV privately…" on screen forever.

Keep `resume.md` in sync with the master resume in Cowork (`OUTPUTS/master-resume/master-resume.md`) — it is the single source of truth every tailored CV is built from.

## Delivery

The primary path is application-owned: the daily run builds one immutable snapshot per delivered job and POSTs them all to the Cloudflare Worker's `/internal/digest` route (HMAC-signed, single-use nonce). The Worker posts one Discord embed per job as the application itself — not a webhook — each with its own Tailor CV button.

Posting is idempotent. A `delivery_claims` row per job key, held under a lease, means a retried run never double-posts the same job. Only jobs the Worker confirms as `posted` or `existing` are recorded in `seen.json`, so a job that never actually made it to Discord is retried the next day instead of silently disappearing.

If the Worker is unreachable, the run falls back to the plain Discord webhook and posts the old-style text digest: grouped by company within Strong matches and Worth reviewing, sorted by fit score, split into sub-2000-character messages so it never hits Discord's limit.

The fallback is deliberately *not* used when the Worker answers but every card fails — posting the same jobs through two channels is worse than posting them late, and nothing is marked seen, so the next run retries them. Instead the run exits non-zero, so a day where the digest reached nobody shows up as a red workflow rather than a green one. A broken run stays loud.

Link verification is evidence, not a source of false negatives. A link marked *inconclusive* means GitHub Actions received a block, rate limit, authentication gate, or network failure; the role remains in the digest because it may open from your browser. Only a definite closure (for example a not-found response or a closed-posting page) is dropped.

Use [the weekly review template](docs/weekly-job-review-template.md) for a two-minute review of saved, applied, noisy, and inaccessible roles. That feedback is deliberately manual: it tells you when to tune the score threshold, profile wording, or filters without allowing the workflow to change selection rules silently.

## Files

**Daily pipeline**

- `digest.py` — the whole engine. Config block and company list at the top, the five ATS fetchers, filters, and ranking in the middle, delivery and the candidates/`--discover` mode at the bottom. One file, no framework.
- `resume.md` — the master resume the on-demand tailoring quotes from. Update it whenever the Cowork master changes.
- `profile.json` — your job-fit profile (blurb, skill keywords, target titles) sent to the ranking model and used by the keyword fallback. Refreshed by hand or via `PROFILE_REFRESH_PROMPT.md`.
- `requirements.txt` — `requests` for every HTTP call, `pypdf` and `reportlab` for building the tailored PDF.
- `discord_delivery.py` — signs and POSTs the daily batch of job cards to the Worker's `/internal/digest` route.
- `jd_fetch.py` — gets the real job description out of a posting URL: the page's `JobPosting` JSON-LD, then the employer's ATS when an aggregator is relisting one, then the page text. Also transcribes a screenshotted posting through a vision model. Shared by the daily run and the tailoring runner, so both read a posting the same way. Nothing in it raises; a failure returns `""`.
- `job_archive.py` — builds job snapshots (including the JD SHA-256), the Discord card/button payload, and writes `jobs/<date>.json`.
- `interactive_runner.py` — runs one on-demand tailoring task: settles which job description it may tailor against (`resolve_jd`: archived and hash-verified, pasted, refetched, or read from screenshots), builds the one-page PDF, reports the result and the description's provenance back to the Worker.
- `resume_source.py` — evidence helpers shared by resume ingestion and tailoring: splits `resume.md` into quotable source blocks and checks a tailored draft's claims against them.
- `resume_ingest.py` — normalizes raw resume text into the form `resume.md` needs, with a preview step before anything is accepted as the new master. Written and tested, but not yet called from anywhere: `/resume` currently answers with a refusal (not wired up yet), and the master resume is updated by editing and committing `resume.md` directly.
- `test_digest.py` — offline tests for the daily pipeline (no network, everything mocked): the six real jobs from 2026-07-31, both scoring bugs, JD fetch, and three failure paths.
- `test_interactive.py` — offline contract tests for the snapshot/tailoring/Worker-callback pipeline.
- `.github/workflows/digest.yml` — the cron (`0 4 * * *` = 07:00 AST). Runs the Python and Worker test suites, runs `digest.py`, then commits `seen.json`, `stats.json`, `boards.json`, and `jobs/` back.
- `jobs/` — one `<date>.json` per digest date: full evidence (title, company, location, URL, score, tier, link status, complete JD text and its SHA-256) for every delivered job. Committed back by the workflow; same-day re-runs append, never rewrite.
- `seen.json` — postings already delivered, keyed by `job_identity()`, plus one `jd:<sha256>` key per delivered description. Created on first run; only the workflow commits it. Entries age out after `STATE_TTL_DAYS`, which is also how the pre-2026-08-21 `jsearch_*` id keys retire on their own.
- `stats.json` — the per-run trend (fetched/matched/new/avg-score) the drift warning reads.
- `boards.json` — employer ATS boards discovered from postings the paid-relister cut discarded, plus the misses and the date each was probed. Read at the top of every run and appended to `COMPANIES` for the fetch loop. Committed back by the workflow like the other state files.

**On-demand tailoring (Cloudflare Worker)**

- `worker/src/index.js` — the Worker: verifies Discord interactions, claims delivery of daily cards idempotently, creates and serves tailoring tasks, opens the paste modal, and posts the PDF back to the private channel.
- `worker/commands.js` — the command set registered with Discord (`/resume`, `/tailor-jd`). Source of truth; `npm run register --prefix worker` applies it. `/resume` is registered but not yet implemented: it answers with an immediate refusal rather than creating a task or dispatching a workflow.
- `worker/schema.sql` — the D1 schema: `tasks`, `deliveries`, `delivery_claims`, `internal_nonces`, `job_snapshots`.
- `worker/wrangler.jsonc` — Worker config: non-secret `vars` (Discord application/guild/channel/user ids, the GitHub repository, the Discord public key) and the D1 binding. No secret values live here.
- `worker/test/interaction.test.js` — the Worker's Node test suite (`node --test`): signature verification, channel/user gating, snapshot hash checks, idempotent delivery claims, nonce handling.
- `worker/package.json` — Worker has no runtime dependencies; `npm test` runs `node --test`.
- `.github/workflows/discover.yml` — `workflow_dispatch` only. Runs `digest.py --discover` against the names you give it and prints ready-to-paste `COMPANIES` lines. Read-only, no secrets; it exists so probing a company's ATS never needs a laptop.
- `.github/workflows/interactive.yml` — triggered by the Worker (`workflow_dispatch` with a `task_id` input) to run `interactive_runner.py` for one tailoring task.

**Historical / reference**

- `tailored/` — tailored resumes from the old, now-removed auto-tailoring pipeline. Historical output, not produced by anything on `main` anymore.
- `JOBS.md` — a dated snapshot of verified job listings from 2026-07-23. Not a living document; don't expect it to track current postings.
- `PROFILE_REFRESH_PROMPT.md` — the monthly prompt used to review and update `profile.json` by hand.
- `docs/weekly-job-review-template.md` — copy this once a week to record which Strong and Review roles were useful, then use the notes to calibrate the workflow deliberately.
- `docs/superpowers/` — design specs and plans from when features were built. Records of a decision at a point in time, not maintained docs — don't edit them to match today's code.
- `docs/2026-07-31-ai-agent-plan.md` — a proposal for a future Claude Code review agent. Not implemented: there is no `.github/workflows/agent.yml`.

## CI/CD hardening

The repository has four explicit GitHub Actions workflows:

- **CI** runs on pull requests, code pushes to `main`, and manual dispatch. It has no production secrets and checks the Python suite, Worker suite, and workflow syntax. State-only digest commits are ignored.
- **job-digest** still targets 07:00 Riyadh (`04:00 UTC`), but GitHub cron is a daily window rather than an exact-time scheduler. It runs preflight tests, executes with read-only access, uploads seven-day generated-state evidence, then uses a separate write-only persistence job to commit only `seen.json`, `stats.json`, and dated `jobs/` snapshots.
- **Interactive job task** runs the same preflight tests before processing the opaque Worker task identifier. Its Worker callback remains the authority for task completion and retry state.
- **Digest freshness watchdog** runs at 11:00 Riyadh and alerts the private Discord webhook if the latest completed digest is not successful for the Riyadh day or `stats.json` lacks that day. It is best-effort and shares GitHub cron's scheduling limits.

All third-party Actions are pinned to immutable commits. Python runtime dependencies are edited in `requirements.in`; regenerate the hash-locked `requirements.txt` with `pip-compile --generate-hashes requirements.in` and install it with `python -m pip install --require-hashes -r requirements.txt`. Dependabot opens weekly update PRs for both pip packages and Action pins.

Failure alerts contain only the workflow, stage, timestamp, and GitHub run URL. They do not include logs, resumes, job data, or secrets. If delivery has succeeded but state persistence fails, do **not** rerun the digest: download the retained state artifact from the failed run, validate/apply it with `workflow_guard.py`, and make a normal scoped commit. Roll back an automation defect by reverting its code/workflow commit; never force-push or delete state history.

## Setup

Two halves: the repository that runs the daily job, and the Cloudflare Worker that owns Discord delivery and on-demand tailoring.

**Repository**

1. New private GitHub repo. Drop these files in (keep the `.github/workflows/` path).
2. Repo Settings, Secrets and variables, Actions. Add, per `.github/workflows/digest.yml`:
   - `DISCORD_WEBHOOK_URL` — the fallback delivery path, used whenever the Worker can't be reached. Not optional: if interactive delivery fails and this isn't set, the run logs FATAL and exits non-zero instead of silently dropping the digest.
   - `INTERACTIVE_WORKER_URL` and `WORKER_CALLBACK_SECRET` — the Worker's address and the shared HMAC secret. Without these the run always falls back to the plain webhook and never posts a Tailor CV button.
   - `RAPIDAPI_KEY` — optional. Without it the digest runs against the five ATS APIs only, no JSearch.
   - `OPENROUTER_API_KEY` — used by both ranking and tailoring. Without it, ranking falls back to deterministic keyword scoring and the Tailor CV button returns the untouched master resume.
   - `OPENROUTER_API_KEY` — optional, and read only by `.github/workflows/interactive.yml`. Without it a Tailor CV click returns your untailored master resume with a note saying why.
   `.github/workflows/interactive.yml` reuses `INTERACTIVE_WORKER_URL` and `WORKER_CALLBACK_SECRET` — same secrets, same names. It also reads four optional repository *variables*: `TAILOR_MODEL` and `TAILOR_FALLBACK_MODELS` retune the tailoring chain, `VISION_MODEL` and `VISION_FALLBACK_MODELS` the chain that reads a screenshotted posting. `digest.yml` reads one more, `MIN_DELIVER_SCORE`, the fit score below which a job is not delivered at all (default 50). All of them change behaviour without a commit; an unset or mistyped one falls back to the code's default rather than failing the run.
3. Actions tab, run the `job-digest` workflow manually once. Read the log.

**Cloudflare Worker**

1. Create a Discord application; point its interactions endpoint URL at the deployed Worker.
2. Create a D1 database and load it from `worker/schema.sql`.
3. Fill in the non-secret `vars` in `worker/wrangler.jsonc`: the Discord application, guild, channel and user ids, the GitHub repository, and the Discord public key (public by design, safe to commit).
4. Set the Worker's secret values with `wrangler secret put` — these are not committed anywhere: `DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `CALLBACK_HMAC_SECRET`, `TASK_ENCRYPTION_KEY`.

**Registering the slash commands.** `worker/commands.js` is the source of
truth for which slash commands exist. `npm run register --prefix worker`
applies it to the Discord application, guild-scoped so it takes effect
immediately. It uses `PUT`, so a command removed from that file is removed
from Discord on the next run. It needs `DISCORD_BOT_TOKEN`,
`DISCORD_APPLICATION_ID` and `DISCORD_GUILD_ID` in the environment — the
latter two are in `worker/wrangler.jsonc`. Run it by hand when the command
set changes; no workflow runs it.

## Test before you trust it (in Claude Code)

- Run `python -m unittest test_digest.py test_interactive.py`. CI runs this offline regression suite before it calls any live API.
- Run `cd worker && node --test`. CI runs this too, right after the Python suite, before `digest.py` ever runs.
- Run `python digest.py` locally with `DISCORD_WEBHOOK_URL` set (and `INTERACTIVE_WORKER_URL` / `WORKER_CALLBACK_SECRET` if you want to exercise the Worker path). Confirm a digest lands in Discord.
- Read the fetch log. Each company prints its job count or a WARN. Confirm HALA, Tamara, Careem, and Lean all return jobs.
- Check the first digest by hand: are the roles actually Saudi, actually senior, actually relevant? Tune `TITLE_INCLUDE` / `TITLE_EXCLUDE` if not.
- Kill the Gemini key for one run and confirm the keyword fallback still posts.
- After changing `worker/commands.js`, run `DISCORD_BOT_TOKEN=… npm run register --prefix worker`. Nothing else changes what Discord shows, and a command left registered with no branch behind it answers "Unsupported command." forever.
- Click **Tailor CV** on a card whose footer says "Job description needed before tailoring". It should either come back with a PDF — the runner read the posting on the second try — or name `/tailor-jd` and the URL it could not read. It should never come back with nothing to do next.
- Then let the cron take over.

## What's deliberately simple

No browser automation, no queue, no framework. Reading a posting means parsing the `JobPosting` metadata a board publishes for search engines, or calling the employer's own ATS API — both of which are stable, documented and cheap. A headless browser would reach a few more pages, cost hundreds of megabytes and minutes on every daily run, and still not get past LinkedIn; that is what `/tailor-jd` and its paste box are for. `digest.py` is one file for the whole daily pipeline. Five ATS readers because the companies genuinely split across them, not for their own sake — each one was added only after a company you actually want forced it, and only after the live API was seen returning data.

There is a database now — D1, five tables — but it's deliberately small: coordination state for the interaction flow (which delivery claimed which job, which task is waiting on which Discord token, a short-lived nonce table). The daily job's own state is still exactly what it always was: JSON files (`seen.json`, `stats.json`, `jobs/`) committed back by the Action. Nothing about the daily pipeline needed a database; the button flow did, because a Discord interaction has to be looked up minutes or days after the run that created it.
