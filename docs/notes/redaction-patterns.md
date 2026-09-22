# Redaction pattern inventory

This is the reviewable inventory for the persistence-boundary redactor. The
implementation is in `twill_redactor.py`; keep changes to the two in sync.

The redactor runs credential replacement before excerpt truncation. Every
configured `content_fences` entry is also matched case-insensitively and
replaced with `<redacted:content-fence>`. Fences are sorted longest-first so a
short entry cannot expose the suffix of a longer fenced name.

| Shape | Marker |
|---|---|
| GitHub classic token (`ghp_`, `gho_`, `ghu_`, `ghs_`, or `ghr_`) | `<redacted:github-token>` |
| GitHub fine-grained token (`github_pat_`) | `<redacted:github-token>` |
| AWS access key (`AKIA` or `ASIA` followed by 16 uppercase alphanumerics) | `<redacted:aws-access-key>` |
| `Bearer` followed by a token-like value | `Bearer <redacted:bearer-token>` |
| OpenAI-style `sk-` token-shaped value | `<redacted:api-key>` |
| Slack `xoxb-`, `xoxa-`, `xoxp-`, `xoxr-`, or `xoxs-` token | `<redacted:slack-token>` |
| A value assigned to `api_key`, `access_token`, `auth_token`, `password`, `secret`, or `token` | `<redacted:secret>` |
| PEM private-key block | `<redacted:private-key>` |

Patterns are deliberately applied to all transcript-derived strings that are
bound to SQLite statements, including identifiers and paths. Excerpt fields
are capped at 240 characters only after this replacement pass.
