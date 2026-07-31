---
name: sonarcloud-findings-inline
description: SonarCloud findings get surfaced inline in chat by the user rather than waiting for me to read PR comments. SonarCloud runs on every PR + push to main; Quality Gate enforces 80% coverage on new code, 0 bugs, 0 vulnerabilities (per CLAUDE.md). Expect findings to arrive mid-session — address in the current commit when reasonable, otherwise note for a follow-up before the PR merges.
type: project
---

# SonarCloud findings — inline channel during refactor sessions

When the user spots SonarCloud findings on a PR or while iterating, they will surface them inline in chat rather than
waiting for me to read the PR comments. SonarCloud runs on every PR + push to main and the Quality Gate enforces 80%
coverage on new code, 0 bugs, 0 vulnerabilities (per `CLAUDE.md`). So expect findings to come in mid-session — address
them in the current commit when reasonable, otherwise note for a follow-up commit before the PR merges.
