# Research-agent delegation contract

Every delegated research / exploration prompt MUST include:

- **Today's date**, and: "do not rely on training data for anything post-cutoff
  — search and verify."
- **Claim tagging:** each claim marked `[CONFIRMED — URL]`, `[REPORTED — URL]`,
  or `[UNCERTAIN]`; shipped vs announced vs rumored kept separate.
- **For local-file exploration:** a file path (and a short quote) for every claim.
- **Output bounds** (e.g. a word range, numbered structure) and a closing
  "N strongest implications, in your judgment" section.
- A caution to check sensational specifics against primary sources (beware
  SEO / AI-content pages).

Process:

- Launch independent agents in parallel; do not re-run their searches yourself.
- Treat subagent output as *Reported, not verified*: spot-check load-bearing
  claims against primary sources before building on them.
- Prefer the read-only `explorer` role for corpus/codebase exploration — it
  returns a path + quote per claim.
