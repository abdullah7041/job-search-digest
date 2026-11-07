# Job-search preferences and source strategy

**Purpose:** This file is the canonical preference guide for the `job-search-digest` project. Read and apply it on every discovery, ranking, digest, and profile-refresh run. It supplements the technical configuration and existing company/ATS registry; it does not replace the repository's verified source adapters or change their implementation by itself.

Last updated: 2026-11-05

## 1. Geography and employment type

- Search for roles located in **Saudi Arabia only**. Do not include UAE, wider GCC, remote-from-other-countries, or other international vacancies.
- Include Saudi cities and roles explicitly open to candidates anywhere in Saudi Arabia. Do not limit discovery to Riyadh alone.
- The user's CV/profile location preference is **Riyadh**. This is a CV presentation preference, not permission to misrepresent a job's actual location or to exclude other Saudi cities.
- Include **full-time** roles only. Exclude internships, temporary placements, part-time roles, and contract-only roles unless the user explicitly changes this rule.
- Do not invent salary, location, employment type, seniority, or eligibility details. Mark unknown details as unknown.

## 2. Role targeting: match evidence, not just titles

Search broadly for roles that genuinely match the candidate's documented experience. Titles are discovery hints, not hard filters. Evaluate the actual responsibilities, required skills, seniority, and qualifications against the resume/profile.

### Primary tracks

1. **AI Product / AI Engineering**
   - Examples: AI Product Engineer, AI Engineer, Applied AI Engineer, LLM Engineer, AI Solutions Engineer, AI Automation Engineer, AI Product Developer, Agentic AI Engineer.
   - Relevant evidence: building AI products and automations; bilingual Arabic-English AI product work; solo-founder product development; practical software engineering and product execution.
2. **Software / Product Engineering**
   - Examples: Software Engineer, Product Engineer, Full-Stack Engineer, Backend Engineer, AI-enabled Software Engineer.
   - Assess the actual stack and delivery expectations against the candidate's documented technical experience. Do not assume experience with tools or frameworks absent from the profile.
3. **Data Analysis / Analytics**
   - Examples: Data Analyst, Product Analyst, Business Analyst (data-focused), BI Analyst, Analytics Engineer where requirements fit.
   - Relevant evidence: data analysis and business-facing analytical work. Be strict about specialist requirements such as advanced statistics, data warehousing, Spark, Kafka, or production ML platforms: do not imply these are established skills unless the resume explicitly supports them.
4. **Adjacent roles**
   - Include product, technical operations, solutions, or program roles only when the job description materially aligns with the candidate's demonstrated software, AI product, data-analysis, or sales experience.
   - Do not broaden into unrelated sales, generic administration, or non-technical roles merely to increase the number of results.

### Candidate facts and accuracy

- The candidate is a software engineer / AI product builder, graduated in 2018, has experience in sales and data analysis, and is a solo founder building Watheq, a bilingual Arabic-English AI resume/application product.
- Use only facts and skills supported by the current `profile.json` and resume. This summary is orientation, not permission to add unverified achievements, metrics, technologies, employers, certifications, or years of experience.
- Do not describe the candidate as a fresh graduate.
- Do not assume Jira or Slack experience unless the current profile explicitly confirms it.
- Do not score a role highly when it has a mandatory qualification, technology, domain, or seniority requirement the candidate does not meet. Separate mandatory requirements from preferences and explain material gaps.

## 3. Preferred discovery and application-source hierarchy

Use this order when finding vacancies:

1. **Employer's official careers website** and the employer's own job listings.
2. **Employer's official ATS application page** (for example, the employer's Greenhouse, Workable, Pinpoint, Lever, or other verified ATS board).
3. **LinkedIn** as a discovery channel. When a listing links to the employer's own application page, use and verify that destination.
4. **Indeed** as a discovery channel and fallback source.

Rules:

- Prefer the employer-controlled vacancy page as the canonical source and application destination.
- LinkedIn and Indeed are useful for discovering vacancies, but do not treat an aggregator copy as more authoritative than the employer's live listing.
- If the employer's official route is unavailable, a native LinkedIn or Indeed application may be used as a fallback when the listing is credible and still open.
- Never claim that an application was submitted merely because a vacancy was found or opened. This repository's digest is for discovery unless an explicitly authorized application workflow is being run.
- Keep the existing ATS/source registry and adapters intact. Add a new company source only after confirming the correct employer identity, official careers/ATS URL, and that the source exposes Saudi vacancies.
- Do not scrape or rely on a guessed ATS tenant, company slug, or URL. Verify it first.

## 4. Target-company list

Actively check these named target companies when relevant Saudi vacancies are available. This is a priority list, not an exclusive list; continue discovering other Saudi employers with strong role fit.

### Priority targets to add/check

- Amazon
- Laverne
- Sanabil Studio

### Other named targets

- DHL
- Tesla
- Tamara
- Tabby
- Google
- CEER

### Existing Saudi startup / employer discovery pipeline

Continue monitoring the existing companies and sources already configured in the repository, including where applicable:

- HALA
- Careem
- Lean
- Foodics
- Salla
- Mozn
- Unifonic
- Jahez
- Zid
- Nana
- Rasan
- Sary

The list above supplements rather than replaces the current `COMPANIES`, ATS configuration, candidate pipeline, and previously verified sources in `digest.py` and related files. Do not remove existing sources simply because they are not repeated here.

For Amazon, DHL, Tesla, and Google, include only vacancies whose stated work location is in Saudi Arabia. For Laverne, Sanabil Studio, CEER, and the Saudi startup pipeline, verify the actual employing entity and role location before including a result.

## 5. Ranking and digest quality

- Rank by **candidate evidence and role fit**, not company prestige, title seniority, keyword density alone, or the number of open vacancies.
- Give strongest consideration to AI product/engineering, AI engineering, software/product engineering, and data-analysis roles.
- A role should not receive a high fit score if a mandatory requirement is clearly unmet. Explain the decisive evidence and gaps.
- Distinguish a confirmed match from a plausible adjacent opportunity. Do not inflate scores to fill a digest.
- Prefer a smaller set of verified, relevant jobs over a large set of weak or speculative matches.
- Do not include expired, closed, duplicate, or unverified vacancies as active opportunities.
- Verify each job at its canonical employer/ATS page as close as practical to digest generation. Record the verification date/time and source URL.
- Deduplicate the same vacancy across employer sites, ATS boards, LinkedIn, and Indeed using requisition ID where available; otherwise compare employer, title, location, and description.
- Clearly label source limitations, inaccessible pages, uncertain location, or missing employment type. Never silently infer them.

## 6. Digest output expectations

For every included vacancy, show:

- Job title and employer
- Saudi city / location (or clearly stated Saudi-wide eligibility)
- Employment type, if confirmed
- Canonical employer/ATS application link
- Discovery source when different from the canonical link (e.g. LinkedIn or Indeed)
- Fit score or fit tier, with a concise evidence-based reason
- The most important mandatory requirement(s) and any material gap
- Verification status/date

Group results by fit and role track rather than presenting a flat list sorted only by company. Include a short **best opportunities** section and state when no strong-fit roles were found; do not pad the digest.

## 7. Operating guardrails

- Treat `profile.json` and the current resume as the source of truth for candidate qualifications. If they conflict with this guide, flag the conflict rather than silently rewriting candidate facts.
- Treat this file as preference guidance, not as a substitute for live vacancy verification or code-level source configuration.
- Do not change scoring code, ATS adapters, schedules, credentials, or application behavior solely because this document was added. If implementation changes are needed to make the digest obey these preferences, propose or implement them as a separate, tested change.
- If a key preference is missing or ambiguous, ask the user rather than guessing.
