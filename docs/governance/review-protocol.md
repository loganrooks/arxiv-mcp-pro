# Review protocol

How changes to this project are reviewed. Pairs with the `pr-review-triage`
skill (`.claude/skills/pr-review-triage/`), which defines the per-finding
verdict vocabulary and the resolve-before-merge mechanics.

## Principles

- **Producer ≠ reviewer.** Whoever wrote a change does not sign off on it.
- **Reviewer comments are signals, not facts.** Every finding is checked against
  the code before it is accepted; a finding can be observation-correct while its
  suggested fix is wrong (see the skill's untrusted-input rules).
- **Proportional review.** Review effort is routed by risk (below), not applied
  uniformly. Mechanical changes are not gated on a human/LLM review; risky
  changes get more eyes, including a second-vendor pass.

## Routing — which review a change gets

| Change class | Review |
|---|---|
| Mechanical / XS — dead-code removal, doc-only, atomic-write fix, formatting | Automated gates only (tests · black · secret-scan · lints). No LLM review. |
| Ordinary scoped logic / output-shape change | One adversarial reviewer pass (holistic) + a focused lens panel. |
| Higher-risk — concurrency/async, rate-limit / external-API handling, secret handling, any change to a tool's default output shape, public-API changes | The above **plus a cross-vendor review** (a second model lineage). |
| Irreversible / high-stakes / unresolved value conflict | Escalate to a human maintainer. |

The routing is **evidence-based**: the cross-vendor row exists because a
concurrency bug in the citation path was caught only by a second-vendor reviewer
after the primary panel had passed it. Routing is reviewed periodically against
the record of which reviews actually catch defects — a class that never yields a
material finding is demoted; one that ships a defect is promoted.

## Per-finding disposition

Every actionable finding gets a typed verdict (`ACCEPTED`, `ACCEPTED_MODIFIED`,
`DEFERRED`, `REJECTED_FALSE_POSITIVE`, `REJECTED_BAD_FIT`, `REJECTED_REGRESSION`,
`OBSOLETE`, `DUPLICATE`) recorded in a parseable `review-verdict` block on the
thread, with reviewer attribution. A PR is not merged with unresolved threads.
See the skill for the full vocabulary, worked examples, and the GitHub
resolve-thread commands.

## The merge gate (enforced on `main`, not left to memory)

For any change routed to a **cross-vendor review** (the higher-risk row above),
all of the following must hold before merge. The parts marked *enforced* are
enforced by GitHub branch protection on `main`, so the loop cannot skip them:

1. **A completed `chatgpt-codex-connector` review on the merge commit.** The
   cross-vendor pass is a second model lineage (Codex/GPT) and catches what a
   Claude-only panel shares blind spots on (it is *why* the higher-risk row
   exists). If the connector wedges or returns nothing — a known runtime risk —
   **re-trigger it**; a both-tier change does not merge on the Claude-side
   review alone. (If the connector is persistently unavailable, that is an
   escalation to the maintainer, not a silent waiver.)
2. **Every review thread resolved with a typed `review-verdict` block**
   *(enforced: "require conversation resolution before merging")*. A thread is
   resolved only after its finding carries a disposition from the 8-verdict
   vocabulary.
3. **All required status checks green** *(enforced: `gates` · `lint` · the
   `test` matrix across 3.11/3.12 × ubuntu/macos/windows)*.

Branch protection on `main` also **requires a pull request before merging** with
0 required approvals: this is a solo-maintainer repo, so a required *approving*
review would deadlock (GitHub forbids approving your own PR). The cross-vendor
connector review is the deliberate substitute for that second set of eyes, and
the conversation-resolution gate is what makes "all its findings dispositioned"
a hard precondition rather than a good intention.

## Gates (enforced by `.pre-commit-config.yaml` + `.github/workflows/ci.yml`)

- `black --check` — formatting.
- `scripts/secret_scan.py` — no credentials committed (the Semantic Scholar key
  lives in the keychain/env, never git).
- `scripts/check_citations.py` — no opaque/unresolvable citation tokens in docs.
- `scripts/check_claim_ledger.py` — claim-ledger files validate against the schema.
- `pytest` — the test suite (pre-push + CI).

## Attribution

The `pr-review-triage` skill is adopted from the `pr-review-journal` plugin and
vendored here per its portability note.
