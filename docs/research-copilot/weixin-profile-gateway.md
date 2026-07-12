# Weixin profile gateway lifecycle

Research Copilot on Weixin uses a **one-profile-one-gateway** architecture: each
human user gets their own Hermes profile, their own iLink Bot credentials, and
their own gateway process. No credential routing, no cron scoping puzzles, no
cross-user state.

This doc covers the day-2 lifecycle: create a profile, start its gateway, watch
it, and turn it off. For a design walkthrough of *why* the boundary is drawn
this way, see the Research Copilot plan under `.hermes/plans/` on the operator
host.

## Create a Weixin bot profile

`hermes weixin-bot create <profile>` bootstraps everything a new user needs:
Research Copilot state (topics, sources, empty history), scripts
(`paper-fetch.py`, `paper-health.py`), the `paper` skill, and the three cron
jobs (`paper-fetcher`, `daily-paper-pick`, `paper-health-report`).

Secret flags land in the profile's `.env` at mode 0600 — values are never
echoed to stdout.

```bash
hermes weixin-bot create alice \
  --weixin-token "$ALICE_ILINK_TOKEN" \
  --weixin-account-id alice-account-id \
  --allowed-user wxid_alice_pc \
  --allowed-user wxid_alice_phone \
  --dm-policy allowlist \
  --clone-config   # optional: seed from your existing profile
```

Environment variables the bootstrap writes into `<profile_home>/.env`:

| Key | Purpose |
|---|---|
| `WEIXIN_TOKEN` | iLink Bot token |
| `WEIXIN_ACCOUNT_ID` | iLink Bot account id |
| `WEIXIN_ALLOWED_USERS` | Comma-separated list of allowed Weixin user ids |
| `WEIXIN_DM_POLICY` | `allowlist` (recommended), `open`, or `off` |

Re-running `create` on an existing profile refuses to clobber existing `.env`
keys unless `--force` is passed. User JSONL history (`candidates.jsonl`,
`recommendations.jsonl`, `interactions.jsonl`) is preserved regardless of
`--force`.

## List Weixin bot profiles

```bash
hermes weixin-bot list
```

Prints a table over every discovered profile:

```
PROFILE  CREDS  GATEWAY  RC   CRON  LAST FETCH            LAST PICK
default  yes    up       yes  yes   2026-07-12T10:01:44Z  2026-07-12T14:30:00Z
alice    yes    down     yes  yes   never                 never
```

Columns:

- **CREDS** — whether `.env` contains non-empty `WEIXIN_TOKEN` and
  `WEIXIN_ACCOUNT_ID`. Never displays the values themselves.
- **GATEWAY** — `up` when the profile's `gateway_state.json` reports any
  adapter as `connected`.
- **RC** — whether Research Copilot has been initialized in the profile.
- **CRON** — whether all three canonical Research Copilot jobs are present.
- **LAST FETCH** — `mtime` of `candidates.jsonl`.
- **LAST PICK** — `recommended_at` of the most recent recommendation.

Pass `--format json` to get machine-readable output.

## Start / stop / inspect the gateway for a profile

Every Hermes CLI command accepts `-p <profile>` to run under that profile's
HERMES_HOME. Gateway subcommands are identical to the default-profile flow:

```bash
hermes -p alice gateway setup      # first-time config wizard
hermes -p alice gateway install    # register a systemd (user) unit
hermes -p alice gateway start      # start the unit
hermes -p alice gateway status     # is it running?
hermes -p alice gateway stop       # stop the unit
```

Each `-p <profile>` invocation resolves to a distinct systemd unit
(`hermes-gateway-alice.service` by convention), so multiple gateways can run
side by side on the same host without sharing a process, credentials, or
Weixin session state.

## Acceptance criteria

The one-profile-one-gateway boundary means all of the following must hold on a
host running multiple bots:

- Starting Alice's gateway does not read Bob's credentials.
- Alice's cron jobs run under Alice's `HERMES_HOME` and never write to Bob's
  research-copilot tree. `tests/research_copilot/test_profile_isolation.py`
  encodes the same property for the bootstrap surface.
- Alice's Weixin bot delivers only Alice's profile outputs.
- Restarting Alice's gateway does not interrupt Bob's session (they are
  independent systemd units and independent iLink Bot sessions).

## Log locations

- Gateway logs: `<profile_home>/logs/gateway.log`, `errors.log`.
- Agent logs (from cron-triggered runs): `<profile_home>/logs/agent.log`.
- `journalctl --user -u hermes-gateway-<profile>.service` for systemd view.

## Rollback

If a new gateway build misbehaves after `git pull` / restart, the fast path is
to check out the last known-good SHA and restart just the affected profile:

```bash
git -C ~/hermes-agent checkout <last-good-sha>
systemctl --user restart hermes-gateway-alice.service
```

Other profiles' gateways are untouched. See
`weixin-ops-runbook.md` for the full incident playbook.
