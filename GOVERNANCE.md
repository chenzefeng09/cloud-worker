# Governance

This document describes how the Agent Platform project is run: who makes decisions, how
those decisions are made, how architectural changes are recorded, how contributors and
maintainers are recognized, and how releases happen. It is intentionally lightweight — the
goal is a clear, predictable process, not bureaucracy.

Agent Platform is built on top of [SaaS Foundation](https://github.com/chenzefeng09/saas-foundation),
which is governed separately as a standalone, reusable enterprise base. Decisions that
affect the contract between the two projects are coordinated across both.

## Roles

- **Users** — anyone running or evaluating Agent Platform. Users contribute by filing
  issues, reproducing bugs, and giving feedback.
- **Contributors** — anyone who proposes a change via a pull request: code, tests, docs,
  or design. There is no paperwork required to become a contributor.
- **Maintainers** — contributors with merge rights who are responsible for the project's
  direction, code quality, releases, and security response. The current maintainers are
  listed in `MAINTAINERS` / `CODEOWNERS` (or, until those exist, are the repository
  administrators on GitHub).

## Decision-making model

Agent Platform follows a **maintainer-led** model with a strong bias toward
consensus.

- **Day-to-day changes** (bug fixes, tests, docs, contained features) are decided through
  normal pull-request review. A change merges once it has at least one maintainer approval
  and passing CI, and no unresolved objections from other maintainers.
- **Lazy consensus.** For most proposals, silence is assent: if a proposal is open for a
  reasonable period (typically a few business days for non-trivial changes) with no
  sustained objection, it is considered accepted.
- **Significant or hard-to-reverse changes** — new services, cross-cutting API or contract
  changes, security-model changes, dependency or runtime changes, or anything affecting
  the SaaS Foundation integration boundary — require an
  [ADR](#architecture-decision-records-adrs) and explicit agreement among maintainers.
- **Resolving disagreement.** Maintainers work toward consensus on the relevant issue,
  pull request, or ADR. If consensus cannot be reached, the decision escalates to the
  project lead, whose role is to break ties so the project keeps moving — not to override
  a working consensus.

All substantive discussion happens in the open on GitHub issues, pull requests, and ADRs,
so the rationale behind decisions is discoverable later.

## Architecture Decision Records (ADRs)

Architecturally significant decisions are recorded as ADRs in
[`docs/adr/`](./docs/adr/). Existing records cover the sandbox runtime choice and the
agent kernel choice.

The process:

1. Open a pull request adding `docs/adr/NNNN-short-title.md` (next sequential number),
   following the structure of existing ADRs: **Context**, **Decision**,
   **Alternatives considered**, **Consequences**, and a **Status** (`Proposed`).
2. Discussion happens on the PR. Maintainers seek consensus.
3. When accepted, the ADR is merged with status **Accepted**. An ADR that later changes
   direction is not deleted — it is marked **Superseded** with a link to its replacement.

Propose an ADR before or alongside a large implementation PR, so the design is agreed
before significant code is written. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the
mechanics.

## Becoming a contributor

Open a pull request. Read [`CONTRIBUTING.md`](./CONTRIBUTING.md) for environment setup,
the make targets, branch/commit conventions, and how CI works. Good first contributions include reproducing and
fixing bugs, improving tests and documentation, and tightening any of the security
controls described in [`SECURITY.md`](./SECURITY.md).

## Becoming a maintainer

Maintainers are invited from the pool of active contributors. There is no fixed quota; the
bar is sustained, high-quality involvement and the trust of the existing maintainers.
Typical signals:

- A track record of merged, well-scoped contributions across more than one area.
- Thoughtful, constructive review of others' pull requests.
- Reliability and good judgment on design discussions and ADRs.
- Adherence to the project's conventions and a respectful, constructive manner.

Any maintainer may nominate a contributor by opening a proposal (issue or PR updating the
maintainers list). The nomination is accepted by consensus of the existing maintainers.
Maintainers who become inactive for an extended period may, by the same consensus process,
be moved to emeritus status; they are welcome to return.

## Releases

- **Versioning.** The project follows [Semantic Versioning](https://semver.org/). While
  pre-1.0, minor versions (`0.x`) may include breaking changes; these are always called
  out in the changelog.
- **Cadence.** Releases are cut as meaningful changes accumulate rather than on a fixed
  calendar. Security and other urgent fixes are released as patch (`0.1.z`) versions as
  needed.
- **Process.** Each release updates [`CHANGELOG.md`](./CHANGELOG.md) (moving items out of
  `[Unreleased]` into a dated, versioned section) and is published as a tagged GitHub
  release. Only the latest `0.1.x` line receives fixes, per [`SECURITY.md`](./SECURITY.md).
- **Coordination with SaaS Foundation.** Changes that alter the Foundation integration
  contract are coordinated with the SaaS Foundation maintainers and noted in the
  changelog.

## Code of Conduct

All participation in the project — issues, pull requests, reviews, and discussions — is
expected to be respectful and constructive. Maintainers are responsible for keeping
interactions civil, and may moderate contributions that are not. Report unacceptable
behavior to <chenzefeng09@gmail.com>.

## Changing this document

This governance document is itself subject to the process it describes: propose changes via
a pull request, to be accepted by maintainer consensus.
