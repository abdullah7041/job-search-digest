# Saudi ATS source audit — 2026-09-27

Checked each candidate's employer identity and public ATS API on 2026-09-27. Only the existing Greenhouse and Workable fetchers were used; no paid API or scraped listing was used. The added roles are currently published on the employer boards, but none is within the digest's seven-day window today. The shared age gate therefore excludes these examples until a posting is newly published.

| Candidate | Verified board | Current Saudi IC role evidence | Decision |
| --- | --- | --- | --- |
| MinIO | [MinIO careers](https://min.io/careers) → [Greenhouse board](https://boards-api.greenhouse.io/v1/boards/minio/jobs?content=false) | `Site Reliability Engineer - Riyadh`, Riyadh, published 2026-09-03. A second Riyadh `Senior Software Engineer` role was published 2026-06-12. | Add `greenhouse/minio`; the current SRE role is software infrastructure IC work. Both dates are outside seven days. |
| Scale AI | [Scale careers](https://scale.com/careers) → [Greenhouse posting](https://job-boards.greenhouse.io/scaleai/jobs/4732394005) | `Senior Full-Stack Software Engineer, (Forward deployed), GPS`, Riyadh, published 2026-09-10. The board also lists a Staff full-stack role in Riyadh. | Add `greenhouse/scaleai`; current senior software IC role verifies fit. Both publication dates are outside seven days. |
| Lucidya | [Lucidya careers](https://www.lucidya.com/) → [Workable posting](https://apply.workable.com/lucidya/j/8120FF802B/) | `Frontend Software Engineer - Saudi Only`, Riyadh, published 2026-09-13; `Site Reliability Engineer - Saudi Only`, Riyadh, published 2026-09-03. | Add `workable/lucidya`; the current engineering roles verify fit. Both dates are outside seven days. |
| Ninja | Workable account `ninja`, checked through `https://apply.workable.com/api/v3/accounts/ninja/jobs` | API returned zero published jobs. | Skip until it exposes a matching Saudi role. |
| Qoyod | Workable account `qoyod`, checked through `https://apply.workable.com/api/v3/accounts/qoyod/jobs` | API returned zero published jobs. | Skip until it exposes a matching Saudi role. |

The Greenhouse counts at audit time were 19 MinIO roles and 203 Scale AI roles. Lucidya's Workable API returned 10 roles on its first page, including the two Saudi engineering roles above. Ninja and Qoyod returned zero. These counts and role dates are an audit snapshot and will change.

## Coverage limits

The run reads all hand-curated companies and at most 50 confirmed discovered boards, rotating the least recently read first. The digest now reports attempted boards out of the full current inventory; boards beyond the cap or the run-time budget are counted as skipped. This is not a claim of complete Saudi-market coverage.
