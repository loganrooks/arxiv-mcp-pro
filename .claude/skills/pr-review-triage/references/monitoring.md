# Monitoring PR Activity

When the orchestrator triggers a review (`@codex review`, push a fix, request CI) and then needs to wait for the response, polling is wasteful and waiting for the user to nudge is rude. The right tool depends on how many notifications you need and whether the wait has a natural end.

## Three patterns

| Pattern | How many notifications | Right tool | When |
|---|---|---|---|
| One signal, known end | 1 | `Bash` with `run_in_background` + `until` loop | "Tell me when CI finishes." "Tell me when Codex has reviewed." |
| Per-occurrence, indefinite | N (unbounded) | `Monitor` with `persistent: true` | "Tell me every time a new comment lands on PR #N during this session." |
| Per-occurrence, known end | N (bounded) | `Monitor` with a loop that exits | "Emit each CI check as it lands, stop when the run completes." |

The single most common mistake is using `Monitor` with `tail -f` or `while true` when you only need one notification. An unbounded command stays armed until timeout even after the event has fired. For "wake me when X happens once," use `Bash` with `run_in_background` and an `until` loop that exits.

## Pattern 1 — wait for one signal (single notification)

Use case: you triggered `@codex review` and need to know when Codex posts. You don't care about intermediate state; just notify on first new review by `chatgpt-codex-connector` since the trigger.

```bash
# Capture the baseline review count NOW so the wait only counts NEW reviews.
PR=8
REPO=loganrooks/tap-n-filter
BASELINE=$(gh pr view "$PR" --repo "$REPO" --json reviews \
  --jq '[.reviews[] | select(.author.login=="chatgpt-codex-connector")] | length')

# Bash run_in_background; the until loop exits when count increments.
until [ "$(gh pr view "$PR" --repo "$REPO" --json reviews \
  --jq '[.reviews[] | select(.author.login=="chatgpt-codex-connector")] | length')" -gt "$BASELINE" ]; do
  sleep 30
done
echo "Codex review landed on PR $PR"
```

Run via `Bash` with `run_in_background: true`. A single completion notification fires when the loop exits. Total cost: one harness notification, no chat noise.

Variant — wait for any reviewer's COMMENTED-or-newer review since a known timestamp:

```bash
SINCE="2026-05-22T22:00:00Z"
until gh pr view "$PR" --repo "$REPO" --json reviews \
  --jq "[.reviews[] | select(.submittedAt > \"$SINCE\")] | length" | grep -qv '^0$'; do
  sleep 30
done
```

## Pattern 2 — watch ongoing PR activity (per-occurrence, indefinite)

Use case: PR is open, you'll be addressing findings as they arrive across multiple reviewer passes. You want a chat notification each time a new comment from CR / Codex lands so you can switch contexts to address it.

```bash
# Emit one line per new comment from the watched reviewers.
PR=8
REPO=loganrooks/tap-n-filter
REVIEWERS='coderabbitai|chatgpt-codex-connector|copilot-pull-request-reviewer\[bot\]'
LAST_SEEN=$(date -u +%Y-%m-%dT%H:%M:%SZ)

while true; do
  gh api "repos/$REPO/pulls/$PR/comments?since=$LAST_SEEN" \
    --jq ".[] | select(.user.login | test(\"^($REVIEWERS)$\")) | \"\(.user.login) commented on \(.path):\(.line // 0): \(.body[0:120])\"" \
    2>/dev/null || true
  LAST_SEEN=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  sleep 30
done
```

Run via `Monitor` with `persistent: true`. Each new comment becomes one chat notification. Stop with `TaskStop` when you're done with the PR.

Important: `|| true` keeps a transient `gh` failure from killing the monitor. Network blips happen.

## Pattern 3 — watch CI to completion (per-occurrence, bounded)

Use case: you pushed a fix; want one notification per check as it completes, and the monitor should stop when all checks have terminal state. No polling after CI is done.

```bash
PR=8
REPO=loganrooks/tap-n-filter
prev=""
while true; do
  s=$(gh pr checks "$PR" --repo "$REPO" --json name,bucket 2>/dev/null) || { sleep 30; continue; }
  cur=$(jq -r '.[] | select(.bucket!="pending") | "\(.name): \(.bucket)"' <<<"$s" | sort)
  # Emit only checks new since last poll.
  comm -13 <(echo "$prev") <(echo "$cur")
  prev=$cur
  # Exit when no checks remain pending.
  jq -e 'all(.bucket!="pending")' <<<"$s" >/dev/null && { echo "CI complete on PR $PR"; break; }
  sleep 30
done
```

Run via `Monitor` (non-persistent). Each check completion is one notification; the final "CI complete" line is the last one.

## Coverage — silence is not success

A monitor whose filter matches only the happy path goes silent when the unhappy path happens. Before arming a monitor for any waited-on state, ask: *if the thing crashed right now, would my filter emit anything?* If not, widen it.

Wrong (silent on crash, timeout, or any non-success exit):

```bash
tail -f deploy.log | grep --line-buffered "Deploy succeeded"
```

Right (alternation covering progress + the failure signatures you'd want to act on):

```bash
tail -f deploy.log | grep -E --line-buffered "Deploy succeeded|Deploy failed|Traceback|FATAL|Killed"
```

For a PR-review wait, the failure signatures include:
- Codex posts "needs environment setup" instead of a review
- CR's quota is exhausted (the "Review skipped" status check)
- The reviewer's GitHub App was uninstalled mid-review
- A reviewer posts a question rather than findings (a thread that needs your reply before the wait makes sense)

The poll-based monitors above survive these by checking concrete state (count of reviews, list of checks) rather than scraping prose.

## Pipe-buffering gotcha

Without `--line-buffered`, grep buffers stdout when output is going to a pipe. Events that look "instant" in interactive mode arrive minutes late through a monitor pipeline.

```bash
# Wrong — buffered
tail -f log | grep "ERROR"

# Right
tail -f log | grep --line-buffered "ERROR"
```

`awk` has `fflush()`, `sed` has `-u`, Python has `python3 -u` or `sys.stdout.flush()` — every tool in the pipe needs explicit flushing.

## Combine with `PushNotification` for high-signal events

`Monitor` events become chat notifications, which the user sees in the transcript. If an event needs the user's *immediate* attention (a critical finding posted, CI failed in a way that blocks merge, a reviewer raised a security concern), `PushNotification` sends a push to the user's device — useful when the user has switched away from the chat.

A reasonable rule of thumb: monitor events are passive (the user sees them next time they check the chat); push notifications are active (the user gets pinged on their phone). Use push for things that change what the user would do next, not for routine status flips.

## Anti-patterns

1. **Unbounded command for single notification.** `Monitor` with `tail -f log | grep -m 1 "Ready"` looks like it should fire once and stop. It doesn't — `tail` keeps running because the log doesn't close, and `grep -m 1` only stops *grep*. The monitor stays armed until timeout. Use Pattern 1 (Bash with `run_in_background` and an `until` loop) instead.

2. **Raw log piping.** `Monitor` with `tail -f huge.log` floods the chat. Monitors that produce too many events are automatically stopped. Filter aggressively at the source.

3. **No transient-failure handling.** `Monitor` with a poll loop that calls `gh api` without `|| true` dies on the first network hiccup. Always swallow transient errors in the poll body.

4. **Too-tight poll interval for remote APIs.** GitHub rate-limits at 5000 req/hr per token. A 1-second poll burns the quota fast. Use 30s+ for remote API polls; 0.5-1s is fine for local checks (file existence, log line presence).

5. **Forgetting to stop.** Persistent monitors keep running until `TaskStop` or session end. If you've moved on from a PR, stop the monitor so the next session doesn't inherit unrelated notifications. Use `TaskList` to see active monitors and `TaskStop` to kill specific ones.

## Quick decision: which tool for the PR-review-triage workflow

| Situation | Use |
|---|---|
| "Tell me when Codex / CR posts on PR #N" | Bash `run_in_background` + `until` loop (Pattern 1) |
| "Tell me about new activity on PR #N while I work on other things" | Monitor persistent (Pattern 2) |
| "Tell me as each CI check finishes; stop when CI is done" | Monitor bounded loop (Pattern 3) |
| "Tell me when `unresolvedReviewThreadCount` hits zero" | Bash `run_in_background` + `until` loop, poll via GraphQL |
| "Tell me when the PR's mergeable state changes" | Bash `run_in_background` + `until` loop |

The skill's `pr-review-triage` workflow uses Pattern 1 most often: trigger a review, wait for the single completion notification, switch back to triage when it lands.
