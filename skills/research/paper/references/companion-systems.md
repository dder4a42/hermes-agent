# Companion Systems

The Research Copilot (`/paper`) is one of three related systems built for WeChat interaction. The other two are built into Hermes core.

## `/s` — Schedule / Timed Reminders (core)

Concrete timed reminders with natural language input. Data in `~/.hermes/thoughts.json[".tasks"]`.

Examples:
- "remind me to try Mixue's new drink tomorrow" → `/s add "Try new drink" --when "tomorrow 2pm"`
- "check server every weekday at 9am" → `/s add "Check server" --when "every weekday at 9am"`

Lifecycle: add → pause/resume → done (auto-done after one-shot fire).

Surfacer: `task-surfacer.py` (no_agent, every 5 min) checks for due tasks, sends WeChat message.

## `/th` — Thought Incubation (core)

Fuzzy ideas that need incubation. Data in `~/.hermes/thoughts.json[".thoughts"]`.

Examples:
- "we should invest in GUI agents" → agent asks "want me to save that as a thought?"
- "maybe OPD + RL could be combined" → agent captures as thought

Lifecycle: active → dormant → archived. Surfacer picks one active thought daily (morning) and sends to WeChat for re-engagement.

## `/paper` — Research Copilot (skill)

Daily personalized paper recommendation. Uses the hybrid pipeline (no_agent fetch + agent pick).

## How They Connect

| | /s (Schedule) | /th (Thought) | /paper (Research) |
|---|---|---|---|
| **Granularity** | Concrete | Fuzzy | Discovery |
| **Auto-capture** | "remind me to…" | "we should…" | N/A (fetch-driven) |
| **Trigger** | Exact time match | Daily pick | Daily scoring |
| **Source** | User explicit | Conversation | Community sources |
| **Storage** | thoughts.json tasks[] | thoughts.json thoughts[] | research-copilot/* |

The three systems are intentionally separate by command entry point but can reference each other:
- A thought about research can feed open_questions into /paper topics
- A paper recommendation can spawn a task ("read this by Friday")
- A completed task can generate a thought ("this paper suggests we look into X")
