# Scheduled personal workflows

The scripts in this directory are small, non-agent cron workers for personal
state already managed by Hermes. They produce text on stdout for cron delivery
and remain silent when no action is due.

## Task surfacer

`task_surfacer.py` reads `<HERMES_HOME>/thoughts.json` and emits due task
reminders. Schema version 1 uses `scheduled_at` as the single scheduling source
of truth; recurrence advances from that absolute timestamp after a reminder.
Legacy `schedule_cron` values are not evaluated independently.

## Thought incubator

`thought_surfacer.py` selects an active thought whose `review.next_review_at`
is due, emits its next-action prompt, records the surfacing event, and applies
progressive unanswered backoff. It respects the `thoughts.incubation` section
of `config.yaml`:

```yaml
thoughts:
  incubation:
    enabled: true
    active_hours:
      start: "09:00"
      end: "21:30"
    max_nudges_per_day: 2
    unanswered_backoff: ["3d", "7d", "30d"]
```

Users manage thoughts through `/th` (or the equivalent CLI command):

```text
/th capture <idea>
/th status
/th show <id>
/th snooze <id> <3d|12h|ISO time>
/th next <id> <clarify|research|learn|track|defer> [prompt]
/th discuss <id>
/th pause <id>
/th done <id>
```

Responses during an active thought discussion reset unanswered backoff and are
recorded in the thought history.

## Migration and safety

`migrate_thoughts.py` upgrades older thought/task stores to schema version 1.
Back up profile state before running migrations against a real profile. All
workers resolve `HERMES_HOME`; a named profile must never read the default
profile's `thoughts.json`.

Focused tests:

```bash
.venv/bin/python -m pytest \
  tests/scripts/test_task_surfacer.py \
  tests/scripts/test_thought_surfacer.py \
  tests/scripts/test_migrate_thoughts.py \
  tests/tools/test_thought_tools_checklist.py -q
```
