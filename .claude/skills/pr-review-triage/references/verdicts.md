# Verdict Vocabulary

The eight verdict values, in canonical form. Use these exact strings — downstream tools and journal entries depend on the spelling.

## The eight verdicts

### `ACCEPTED`

The reviewer's suggestion was applied verbatim or near-verbatim.

**When to use:** the suggested diff (or the prose's clear intent) became the fix. The change is small enough that "near-verbatim" is honest — e.g., variable renames, formatting adjustments.

**Required fields:** `commit`.

**Example:**

````markdown
```review-verdict
verdict: ACCEPTED
commit: 7ac3816
finding_category: ui/binding-staleness
reviewer: chatgpt-codex-connector
```

Picker selection getter now reads live; accessibility-value extracted into a helper for the same reason. Diff matches the suggestion at EffectControlsView.swift:142-156.
````

---

### `ACCEPTED_MODIFIED`

The reviewer's underlying observation was correct, but the fix is different from what they suggested. This is the most common verdict for orchestrators who think before applying.

**When to use:** you agreed the issue is real, traced it to the root cause, and the fix you wrote covers more (or covers something the suggestion missed). Include enough in `notes` for a future reader to understand the divergence.

**Required fields:** `commit`, `notes`.

**Example:**

````markdown
```review-verdict
verdict: ACCEPTED_MODIFIED
commit: 14b240b
finding_category: source-resolution-correctness
reviewer: chatgpt-codex-connector
notes: PID-first match per the suggestion, plus bundle-ID fallback for the relaunch-between-pick-and-start case the original didn't cover. Edge case where both PID and bundle are nil tightened in 814a751.
```
````

---

### `DEFERRED`

The issue is real, but you've intentionally not fixed it in this PR.

**When to use:** the fix requires infrastructure that doesn't exist yet (build config, CI workflow, new dependency); the proper fix is large enough to warrant its own PR; or the deviation is documented in an ADR as an accepted env-bound exception. Always link the ADR or uncertainty-log entry.

**Required fields:** `notes`. `commit` may be present if the deferral is *partially* addressed.

**Example:**

````markdown
```review-verdict
verdict: DEFERRED
finding_category: swift/typed-errors
reviewer: coderabbitai
notes: Tracked as U-011 in docs/decisions/uncertainty-log.md. Promoting .graph/.parameter/.preset error variants to typed payloads is the right model for V0.2; collapsing to a single string-payload variant is acceptable for V0.1 because no consumer programmatically discriminates between them.
```
````

---

### `REJECTED_FALSE_POSITIVE`

The finding does not describe a real problem.

**When to use:** the reviewer misread the code, the snippet they cited doesn't have the property they claim, or the "bug" is fundamental misunderstanding of how the language/framework behaves. Be careful with this verdict — automated reviewers are usually directionally right even when the specifics are wrong. Prefer `REJECTED_BAD_FIT` if the *suggestion* is wrong but the *observation* points at something.

**Required fields:** `notes`.

**Example:**

````markdown
```review-verdict
verdict: REJECTED_FALSE_POSITIVE
finding_category: concurrency/data-race
reviewer: coderabbitai
notes: Property is @MainActor-isolated; the access path the finding describes can only happen on the main actor. No race possible. AppViewModel.swift:142-150.
```
````

---

### `REJECTED_BAD_FIT`

The underlying observation might be reasonable in general, but the suggested fix conflicts with a project-local convention, an ADR, or a constraint the reviewer can't see.

**When to use:** automated reviewers apply generic patterns from their training data. When that pattern conflicts with a local choice — public API surface, language convention enforced by your style guide, accessibility constraint codified in an ADR — the verdict is `REJECTED_BAD_FIT` with a clear reason. This is not a put-down; it's documenting the conflict for the next maintainer.

**Required fields:** `notes`.

**Example:**

````markdown
```review-verdict
verdict: REJECTED_BAD_FIT
finding_category: style/import-ordering
reviewer: coderabbitai
notes: Project convention across Sources/UI/ is pure alphabetical imports (verified by grep). Reviewer's "frameworks before internal modules" suggestion would diverge from every other UI file. Worth a project-wide pass in a separate PR if the team adopts that convention; not appropriate as a one-file change.
```
````

---

### `REJECTED_REGRESSION`

Applying the suggestion would break something verifiable.

**When to use:** you actually tried the suggested fix (or are confident enough about its consequences) and it breaks a test, a type-check, or existing behavior. Cite the regression evidence. This is rarer than `REJECTED_BAD_FIT` — usually the suggestion is *plausible* but conflicts; a true regression is when applying it visibly breaks the build.

**Required fields:** `notes`.

**Example:**

````markdown
```review-verdict
verdict: REJECTED_REGRESSION
finding_category: lifecycle/preset-reattach
reviewer: chatgpt-codex-connector
notes: Tried applying the suggested early-return; ControllerStateMachineTests.test_preset_reattach_after_failed_restore fails because the original graph never gets restored. Kept the existing rollback path; tightened the error log instead (see commit X).
```
````

---

### `OBSOLETE`

The finding was already resolved by an earlier commit; the issue no longer reproduces.

**When to use:** the reviewer flagged something that an intermediate commit fixed (often the round-1 → round-2 case). Verify by reading the file at the cited line — if the issue is gone, this is the verdict. Cite the commit that fixed it.

**Required fields:** `commit`.

**Example:**

````markdown
```review-verdict
verdict: OBSOLETE
commit: 7ac3816
finding_category: ui/dependency-injection
reviewer: chatgpt-codex-connector
notes: AddEffectButton iterates viewModel.availableEffectTypes which forwards to the injected EffectNodeRegistry; already in place at the original Phase 3 commit, no separate fix needed.
```
````

---

### `DUPLICATE`

Same issue tracked on another thread; point at that thread.

**When to use:** two reviewers (or the same reviewer in two passes) flag the same issue at different anchor points. Pick the primary thread (usually the earlier one), apply the verdict there, mark the others as `DUPLICATE` pointing at the primary.

**Required fields:** `notes` (with the primary thread reference).

**Example:**

````markdown
```review-verdict
verdict: DUPLICATE
finding_category: lifecycle/graph-detach
reviewer: chatgpt-codex-connector
notes: Duplicate of PRRT_kwDOSjmLjM6D4kdL. Disposition lives there; this thread mirrors it.
```
````

---

## Field rules summary

| Field | When required |
|---|---|
| `verdict` | Always |
| `commit` | `ACCEPTED`, `ACCEPTED_MODIFIED`, `OBSOLETE`. Optional on `DEFERRED` if partially addressed. |
| `notes` | `ACCEPTED_MODIFIED`, `DEFERRED`, `REJECTED_*`, `DUPLICATE`. Optional on `ACCEPTED` and `OBSOLETE`. |
| `reviewer` | Optional; auto-derived from the thread's first author when absent. |
| `finding_category` | Optional but recommended; free-form (e.g., `style/imports`, `lifecycle/cleanup`, `concurrency/race`). |

## Reconsidered verdicts

A later reply can supersede an earlier verdict — e.g., what was initially `REJECTED_BAD_FIT` turns out to be a real bug after deeper investigation. Use a separate fence:

````markdown
```review-verdict-reconsidered
verdict: ACCEPTED_MODIFIED
commit: <new sha>
notes: Earlier REJECTED_BAD_FIT was premature; the convention I cited turned out to apply only to executable targets, not the UI module. Applied the modified fix in <sha>.
```
````

Downstream tooling records both with timestamps; the latest reconsidered verdict wins for `current verdict` purposes, but the history is preserved.

## Disposition decision tree

When you're unsure which verdict to pick, walk through these:

1. **Does the finding reproduce against current code?**
   - No → `OBSOLETE`.
   - Yes → continue.
2. **Is the same issue already addressed on another thread?**
   - Yes → `DUPLICATE`, point at the primary.
   - No → continue.
3. **Is the finding describing a real problem?**
   - No → `REJECTED_FALSE_POSITIVE`.
   - Yes → continue.
4. **Does the suggested fix conflict with a local convention or ADR?**
   - Yes → `REJECTED_BAD_FIT`, cite the convention.
   - No → continue.
5. **Have you tried the suggested fix and confirmed it breaks something?**
   - Yes → `REJECTED_REGRESSION`, cite the breakage.
   - No → continue.
6. **Are you fixing it in this PR?**
   - No → `DEFERRED`, link the ADR / uncertainty-log entry.
   - Yes → continue.
7. **Did you apply the suggestion verbatim?**
   - Yes → `ACCEPTED`, cite the commit.
   - No → `ACCEPTED_MODIFIED`, cite the commit and explain the divergence.

## Anti-pattern: verdict-shopping

The verdict describes the disposition that actually happened; it does not pre-justify an outcome the orchestrator wishes had happened. If the temptation is to write `REJECTED_BAD_FIT` because the fix is inconvenient, but there is no real local convention to cite, the honest verdict is `DEFERRED` (with a real reason in `notes`) or `ACCEPTED_MODIFIED` (with a smaller fix that's actually applied).

A repo whose journal shows 80% `REJECTED_BAD_FIT` is signalling something: a deeply mismatched reviewer, or an orchestrator who's choosing convenience over correctness. The journal will surface the pattern even when the individual dispositions look defensible.
