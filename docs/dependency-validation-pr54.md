# PR #54 dependency validation

Validated on 2026-11-07 against exact commit
`c70b1bf7fa090efc8212298de800f90dff526270` from
`origin/dependabot/pip/pypdf-6.21.4`.

The change raises the direct dependency floor from `pypdf>=5.0` to
`pypdf>=6.21.4` and updates the locked version from `6.15.0` to `6.21.4`,
including the wheel and source hashes. No other locked package changed.

## Artifact and environment checks

[PyPI release metadata](https://pypi.org/pypi/pypdf/6.21.4/json) was fetched
on the validation date. Both published artifacts were not yanked, required
Python `>=3.9`, and matched the two hashes in `requirements.txt`:

| Artifact | SHA256 |
| --- | --- |
| `pypdf-6.21.4-py3-none-any.whl` | `33e4fb41ca352810eb4e9838aa0d82d6888a5f8bf682f43e99cce5faa666255c` |
| `pypdf-6.21.4.tar.gz` | `c8817e3f888d47a087872ded69812c1ef30cacb871d6e053e0c00bbaf0bc2163` |

The exact Git tree was exported to a temporary directory using `git archive`.
A temporary virtual environment used Python
`3.13.7 (tags/v3.13.7:bcee1c3, Aug 14 2025, 14:15:11) [MSC v.1944 64 bit (AMD64)]`
on Windows. The environment was created with `--system-site-packages`, then
all locked runtime dependencies were explicitly installed into the environment:

```text
python -m pip install --ignore-installed --require-hashes -r requirements.txt
```

The install exited successfully. Package metadata checks confirmed that all
eight locked distributions came from the temporary environment, at exactly the
locked versions: certifi 2026.7.22, charset-normalizer 3.4.9, idna 3.18,
pillow 12.3.0, pypdf 6.21.4, reportlab 5.0.0, requests 2.34.2, and urllib3 2.7.0.
An import check confirmed pypdf 6.21.4 loaded from that environment.
No installed system packages were changed.

## Regression checks

The following command ran from the exported PR #54 tree with the temporary
environment's interpreter:

```text
python -X utf8 -m unittest test_digest.py test_interactive.py test_workflow_guard.py test_fit_check.py test_eval.py test_strict_alerts.py
```

Result: exit code 0; **655 tests in 52.925 seconds, OK (skipped=1)**.
The existing skip was `test_workflow_guard.py`'s symlink test because symlink
creation was unavailable on this Windows environment. Existing PDF regression
checks exercised `PdfReader`, text extraction, one-page resume rendering,
education content, highlight delivery, and exclusion of gap notes from PDFs.

The version-specific
[pypdf extraction API documentation](https://github.com/py-pdf/pypdf/blob/6.21.4/docs/modules/PageObject.rst)
was consulted through Context7. No application compatibility patch was required
by the observed results.

## Scope of the evidence

This qualifies the exact PR #54 dependency change with the existing Python
regression suite. It does not establish the result of combining later PR #53
changes, deployment, live delivery, model output quality, or native search and
saved-byte fidelity in Arabic PDFs. Those require their own checks.

## Combined repair and preference compatibility

On 2026-11-07 the same exact pypdf6.21.4 environment ran PR53's complete repair/preference suite: 706 tests passed with the same single Windows symlink skip. Worker tests passed50/50 and its deployment dry-run passed. PR54's own CI was also rerun and passed on the unchanged c70b1bf head. These are preparation checks, not production-delivery evidence.
