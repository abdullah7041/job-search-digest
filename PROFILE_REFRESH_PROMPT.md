# Job-fit profile refresh

Used monthly (via a scheduled nudge) and any time you want to update `profile.json`
by hand. Answer these, then have Claude update `profile.json`, commit it, and
update your background memory.

1. What's your current role/title, and has it changed since last time?
2. What skills or technologies have you picked up or leaned into recently that
   aren't reflected yet (new languages, frameworks, domains)?
3. What roles/titles are you actually targeting now — same as before, or
   shifted (seniority, function, industry)? Which **track** does each belong to,
   and what in the resume evidences it?
4. Has anything changed about your education or credentials — a degree started
   or finished, a certification that a posting would count as a qualification?
5. Anything from the old profile that no longer applies and should be dropped?

## Applying the answers

- `blurb` — a short paragraph combining role, strengths, and what you want,
  written for a scoring model. This is the only place the model learns what you
  have actually built, so name the products, the stacks and the numbers. Keep
  the seniority and location constraints: they are what stops Lead/Staff and
  relocation roles scoring 90+.
  Keep the **EDUCATION sentence** too. 42% of the postings this digest has
  archived state a degree as a minimum qualification, so what you do and do not
  hold is a constraint on the same footing as the Saudi Arabia rule, and leaving
  the model to infer it from the resume's education section is what let a
  posting with an unmet minimum qualification score 91.
- `skill_keywords` — lowercase single words/short phrases for the no-model
  keyword fallback (`digest.py`'s `keyword_score()`). It matches against the job
  **title only**, so every entry has to be something that can plausibly appear
  in a title. A phrase like "llm evaluation" can never match one and is dead
  weight; "llm" matches constantly.
  A keyword matches at a **word start**, and the suffix is left free — so
  `agent` covers `agentic` and `data` covers `datacentre`, but `ml` no longer
  fires on `AML` and `api` no longer fires on `Scraping`. That makes a keyword
  which is a common letter-run inside other words (`ai`, `ml`, `api`) safe to
  keep in the list.
- `tracks` — replaces the old flat `target_titles`. Each track is `name`,
  `titles` and `evidence`, and `rank_jobs()` asks the ranker to pick the one
  track a posting belongs to and judge it against that track's evidence.
  A flat list said what you want and nothing about which part of the resume
  backs it, so a Senior Data Analyst posting was scored against a profile that
  opens with LLM systems.
  Write `evidence` honestly, including what a track does **not** cover — the
  data track naming the absence of Spark, Kafka and warehouse ETL is doing more
  work than the titles beside it.
  You do not need a second resume for a second track. One master resume is the
  evidence; the per-job document is what the Tailor CV button produces at apply
  time.
