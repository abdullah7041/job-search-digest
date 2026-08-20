# Job-fit profile refresh

Used monthly (via a scheduled nudge) and any time you want to update `profile.json`
by hand. Answer these, then have Claude update `profile.json`, commit it, and
update your background memory.

1. What's your current role/title, and has it changed since last time?
2. What skills or technologies have you picked up or leaned into recently that
   aren't reflected yet (new languages, frameworks, domains)?
3. What roles/titles are you actually targeting now — same as before, or
   shifted (seniority, function, industry)?
4. Anything from the old profile that no longer applies and should be dropped?

## Applying the answers

- `blurb` — a short paragraph combining role, strengths, and what you want,
  written for a scoring model. This is the only place the model learns what you
  have actually built, so name the products, the stacks and the numbers. Keep
  the seniority and location constraints: they are what stops Lead/Staff and
  relocation roles scoring 90+.
- `skill_keywords` — lowercase single words/short phrases for the no-model
  keyword fallback (`digest.py`'s `keyword_score()`). It matches against the job
  **title only**, so every entry has to be something that can plausibly appear
  in a title. A phrase like "llm evaluation" can never match one and is dead
  weight; "llm" matches constantly.
- `target_titles` — a list of role titles/seniority you're targeting now;
  appended onto the blurb automatically by `rank_jobs()`.
