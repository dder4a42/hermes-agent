# `/paper` Feedback Loop Notes

Session learning: the Research Copilot needs an explicit Weixin/CLI interaction loop before multi-user onboarding is valuable. A daily pick without feedback cannot learn user taste.

## Minimum command set

Implement the same behavior in CLI and gateway/Weixin, backed by shared profile-scoped code:

- `/paper topics` — list active/dormant topics and priorities
- `/paper history [n]` — recent recommendations with ids
- `/paper save <id>` — append a `save` interaction and mark candidate `saved`
- `/paper skip <id> [reason]` — append a `skip` interaction and mark candidate `skipped`
- `/paper read <id>` — append a `read` interaction and mark candidate `read`
- `/paper feedback <id> <text>` — append free-form feedback
- `/paper health` — summarize recent recommendation/feedback state
- `/paper now` — should eventually trigger a manual pick, but guard against duplicate recommendations and Weixin spam

## Storage rules

Reusable storage should live in a profile-safe module and use `get_hermes_home()`:

- `research-copilot/topics.json`
- `research-copilot/candidates.jsonl`
- `research-copilot/recommendations.jsonl`
- `research-copilot/interactions.jsonl`
- `research-copilot/config.json`

Recommended helper API:

- `get_data_dir()`
- `ensure_data_dir()`
- `load_topics()` / `save_topics()`
- `load_config()`
- `append_interaction(record)`
- `read_recommendations(limit=10)`
- `find_recommendation(item_id)`
- `update_candidate_status(item_id, status)`

## Output style for Weixin

The user confirmed a too-simple link list was readable but not useful. The digest should include:

- one-line thesis
- why it matters to the user's research agenda or recipient
- what is new
- connection to open questions
- comparative/meta-trend analysis
- suggested action
- feedback commands with the recommendation id

Use plain text or Markdown-lite. If delivery fails but a simple message succeeds, suspect formatting/length; resend a plain-text version first, then add insight while keeping formatting conservative.

## Testing pattern

Use TDD:

1. Write storage tests against temp `HERMES_HOME`.
2. Verify failures for missing module/handler.
3. Implement minimal storage and command handler.
4. Add CLI/gateway thin wrappers that call the shared command handler.
5. Run targeted tests with `scripts/run_tests.sh`.
