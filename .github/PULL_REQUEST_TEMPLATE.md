<!--
Thanks for contributing to Agent Platform!
Please fill out the sections below. PRs that leave the checklist incomplete may be slower to review.
-->

## Summary

<!-- What does this PR change, and why? Keep it concise. -->

## Type of change

<!-- Mark all that apply with an "x". -->

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behaviour or APIs)
- [ ] Documentation only
- [ ] Refactor / internal change (no functional change)
- [ ] Build, CI, or tooling
- [ ] Security-sensitive change (see the note at the bottom)

## Affected component(s)

<!-- e.g. bff, session-orchestrator, agent-scheduler, sandbox-pool, app-registry, skill-store,
     mcp-registry, tool-broker, knowledge-ingest, cron-runner, web-fetch-gateway,
     web frontend, agentscope-ext / sandbox, deploy. -->

## Linked issues

<!-- Use "Closes #123" / "Fixes #123" / "Relates to #123" to auto-link. -->

Closes #

## Testing done

<!-- Describe how you verified this change. Include the commands you ran and their outcome. -->

- [ ] `make lint` (ruff + mypy) passes
- [ ] `make test` (pytest across affected services) passes
- [ ] `make conformance` (or `conformance-docker` / `conformance-static`) passes where relevant
- [ ] Frontend checks pass where relevant (`make web-install`, lint, Vitest)
- [ ] Manual verification (describe below)

<!-- Notes on manual testing, edge cases, or anything reviewers should reproduce: -->

## Checklist

- [ ] Tests have been added or updated to cover the change.
- [ ] Documentation has been updated (README / ARCHITECTURE / runbooks / ADRs), including the
      `zh-CN` counterparts where the English docs changed, and they remain content-synced.
- [ ] No secrets, tokens, credentials, or private filesystem paths are committed (logs and
      fixtures included).
- [ ] The change follows the project conventions (CONVENTIONS.md) and the existing
      `foundation_sdk` integration patterns.
- [ ] Public-facing changes preserve known nuances accurately (e.g. dev-vs-container ports,
      planned-vs-implemented features) — no overclaiming.

## Security-sensitive changes

<!--
Complete this section ONLY if your change touches an isolation, authentication, authorization,
egress, secret-handling, or capability-lease boundary — for example the tool-broker,
web-fetch-gateway SSRF controls, sandbox runtimes, per-run lease/token minting,
docker-socket-proxy scopes, DLP enforcement, or any egress-network configuration.
-->

- [ ] This change does **not** weaken default-deny egress, secret isolation, or the per-run
      capability-lease model.
- [ ] New external inputs are validated and fail closed on error.
- [ ] If this affects a security boundary, a maintainer has been requested as a reviewer.

> If you believe you have found a vulnerability, do **not** open a public PR or issue describing it.
> Follow the coordinated-disclosure process in [SECURITY.md](./SECURITY.md) instead.
