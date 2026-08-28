# Weixin Personal Bot + Research Copilot Profile Pattern

Session learning: current Weixin/iLink bot is best treated like a personal File-Transfer-Assistant-style bot, not a public multi-friend bot. The scalable product pattern is therefore one user per Hermes profile and one gateway process per Weixin bot.

## Recommended architecture

```text
User A
  ↔ Weixin Bot A / iLink account A
      ↔ hermes -p user-a gateway
          ↔ ~/.hermes/profiles/user-a/
              config.yaml
              .env with WEIXIN_TOKEN / WEIXIN_ACCOUNT_ID
              cron jobs
              memory
              research-copilot/
              thoughts.json
              sessions
```

Do the same for User B with a separate bot credential and profile. Do **not** share Weixin credentials across profiles.

## Why not multiplex first?

Hermes has code paths for `gateway.multiplex_profiles`, but the safer next stage is one gateway process per profile because:

- Weixin personal bots are single-user in practice.
- Profile isolation naturally scopes cron, memory, paper topics, thought state, sessions, and credentials.
- Operational failures are easier to debug per user.
- Profile-aware cron under one multiplexed gateway needs E2E validation before relying on it.

Revisit single-gateway multiplex only after two or more real profile gateways run reliably and multiple gateway processes become an operational burden.

## Bootstrap checklist for a new Weixin Research Copilot user

1. Create profile:
   `hermes profile create <user> --clone-config`
2. Write credentials to that profile's `.env` only:
   - `WEIXIN_TOKEN=...`
   - `WEIXIN_ACCOUNT_ID=...`
   - optionally `WEIXIN_ALLOWED_USERS=...`
3. Initialize profile-local Research Copilot files:
   - `research-copilot/config.json`
   - `research-copilot/topics.json`
   - `research-copilot/research_profile.json`
   - `research-copilot/source_registry.json`
   - empty JSONL files: `candidates`, `recommendations`, `interactions`
4. Install/copy paper skill and scripts into that profile.
5. Create profile-local cron jobs:
   - `paper-fetcher` at `0 6,18 * * *`, `deliver=weixin`
   - `daily-paper-pick` at `30 8 * * *`, `deliver=weixin`, skill `paper`
   - `paper-health-report` at `0 9 * * 6`, `deliver=weixin`
6. Start the gateway:
   `hermes -p <user> gateway install && hermes -p <user> gateway start`

## Implementation note

Any reusable Research Copilot code must resolve paths through `get_hermes_home()` so it is profile-safe. Avoid `Path.home() / ".hermes"` in reusable modules. User-local dogfood scripts can exist temporarily, but upstreamable code should be profile-safe.
