# Weixin Research Copilot — ops runbook

> **Legacy operational notes.** Cron names, storage files, and profile-local
> script behavior below predate the SQLite Research Library. Start with
> [`research_copilot/README.md`](../../research_copilot/README.md) and verify
> the live job list with `hermes -p <profile> cron list` before applying these
> recipes.

Recipes for common day-2 operations on a host running one or more Weixin bots
backed by Research Copilot. Every command runs on the host that owns the
profile — no cross-host coordination is required.

## Add a user

```bash
hermes weixin-bot create bob \
  --weixin-token "$BOB_ILINK_TOKEN" \
  --weixin-account-id bob-account-id \
  --allowed-user wxid_bob_pc \
  --dm-policy allowlist
```

Then start Bob's gateway:

```bash
hermes -p bob gateway install
hermes -p bob gateway start
hermes -p bob gateway status   # confirm it's up
```

Bob's cron jobs are already scheduled by the bootstrap; the first
`paper-fetcher` run fires at the next 06:00 or 18:00.

## Rotate a Weixin token

The safest path is to overwrite just the token key without touching the rest of
the `.env`:

```bash
hermes weixin-bot create bob \
  --weixin-token "$NEW_ILINK_TOKEN" \
  --force
systemctl --user restart hermes-gateway-bob.service
```

`--force` allows the existing `WEIXIN_TOKEN` line to be replaced. Other keys
(`WEIXIN_ACCOUNT_ID`, `WEIXIN_ALLOWED_USERS`, `WEIXIN_DM_POLICY`) are left
untouched because they weren't passed. Tokens are never printed by the CLI.

If you'd rather edit by hand:

```bash
$EDITOR ~/.hermes/profiles/bob/.env       # replace WEIXIN_TOKEN=<value>
chmod 600 ~/.hermes/profiles/bob/.env     # verify mode
systemctl --user restart hermes-gateway-bob.service
```

## Restart one user's gateway

```bash
systemctl --user restart hermes-gateway-bob.service
journalctl --user -u hermes-gateway-bob.service -n 100 --no-pager \
  | grep -Ei 'error|traceback|connected|ready'
```

Other users' gateways are independent processes and are unaffected. If systemd
isn't in use for that profile:

```bash
hermes -p bob gateway stop
hermes -p bob gateway start
```

## Inspect one user's cron jobs

```bash
hermes -p bob cron list
```

Expect three Research Copilot jobs plus whatever else the user has scheduled:

```
NAME                   SCHEDULE       DELIVER  STATUS
paper-fetcher          0 6,18 * * *   weixin   enabled
daily-paper-pick       30 8 * * *     weixin   enabled
paper-health-report    0 9 * * 6      weixin   enabled
```

Run a job on demand:

```bash
hermes -p bob cron run daily-paper-pick
```

## Diagnose delivery failure

1. Confirm the gateway is up: `hermes -p bob gateway status`.
2. Check the adapter's connection state:
   ```bash
   jq '.weixin.state' ~/.hermes/profiles/bob/gateway_state.json
   ```
   Should be `"connected"`. Any other value is the failure.
3. Tail the gateway log:
   ```bash
   tail -f ~/.hermes/profiles/bob/logs/gateway.log
   ```
4. If a cron delivery failed, tail the agent log:
   ```bash
   tail -f ~/.hermes/profiles/bob/logs/agent.log
   ```
5. Verify credentials are set (never prints the values themselves):
   ```bash
   hermes weixin-bot list | grep '^bob'
   ```

Common causes:
- Token rotated on the iLink Bot side but not updated locally → rotate.
- Weixin session expired (uncommon; iLink Bot handles session refresh) →
  restart the gateway.
- Network unreachable / GFW proxy down → set
  `RESEARCH_COPILOT_HTTP_PROXY` per-profile.

## Handle iLink rate limits

The gateway's Weixin adapter chunks long messages and deduplicates repeats.
If you still see rate-limit errors:

1. Reduce daily volume: turn the `paper-fetcher` schedule from `0 6,18 * * *`
   to `0 6 * * *` (once daily) via `hermes -p bob cron edit paper-fetcher`.
2. Widen the `daily-paper-pick` threshold in `<profile_home>/research-copilot/config.json`
   (raise `threshold` from `0.65` to `0.72`) so fewer picks push through.
3. Pause the fetcher entirely: `hermes -p bob cron pause paper-fetcher`.
   The profile continues to accept `/paper` commands; only the automated
   push slows down.

## Disable a user without deleting their profile

```bash
hermes -p bob gateway stop
systemctl --user disable hermes-gateway-bob.service
hermes -p bob cron pause paper-fetcher
hermes -p bob cron pause daily-paper-pick
hermes -p bob cron pause paper-health-report
```

The profile home, `.env`, and all Research Copilot state remain on disk. Bring
the user back with:

```bash
hermes -p bob cron resume paper-fetcher
hermes -p bob cron resume daily-paper-pick
hermes -p bob cron resume paper-health-report
systemctl --user enable --now hermes-gateway-bob.service
```

## Export a user profile

The profile tree under `~/.hermes/profiles/<name>/` contains everything Bob
needs to migrate to another host:

```bash
tar -C ~/.hermes/profiles -czf bob-profile-backup.tgz bob
```

To restore on another host:

```bash
tar -C ~/.hermes/profiles -xzf bob-profile-backup.tgz
chmod 600 ~/.hermes/profiles/bob/.env
hermes -p bob gateway install
hermes -p bob gateway start
```

The `.env` file contains secrets — treat the tarball as sensitive material.
Prefer `age`, `gpg`, or your organization's secret manager if the backup will
travel outside the host.

## Rollback: pin the repo to a known-good SHA

When a code change breaks a profile's gateway:

```bash
cd ~/hermes-agent
git rev-parse HEAD > /tmp/hermes-broken-sha
git checkout <last-good-sha>
systemctl --user restart hermes-gateway-bob.service
```

Roll forward once the fix lands:

```bash
git checkout main
git pull
systemctl --user restart hermes-gateway-bob.service
```

If a `.env` overwrite corrupted state, the pre-write value is not preserved by
the writer (writes are atomic but do not keep prior versions). Restore from
your last profile backup — see "Export a user profile" above.

## Verify profile isolation

`tests/research_copilot/test_profile_isolation.py` is the canonical safety net.
Run it any time you make changes to bootstrap, storage, or cron:

```bash
scripts/run_tests.sh tests/research_copilot/test_profile_isolation.py
```

All seven cases should pass. If any fail, do not deploy the change — a
regression on this file means one user's Research Copilot state can leak into
another user's tree.
