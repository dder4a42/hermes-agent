# Gmail Newsletter Setup

Fetch pipeline reads newsletters from Gmail ResearchFeeds label via IMAP.

## One-Time Setup

1. Enable 2FA → generate App Password at myaccount.google.com/apppasswords (Mail + "hermes")
2. Store in `~/.hermes/.env`: `GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx`
3. Gmail: create label "ResearchFeeds" (sidebar → scroll → Create label)
4. Gmail Settings → Labels → ResearchFeeds → Show in IMAP
5. Settings → Filters → For each newsletter, create filter applying ResearchFeeds label

## Supported Filter Rules

| Newsletter | From |
|-----------|------|
| HF Daily Papers | notifications@huggingface.co |
| AlphaSignal | newsletter@alphasignal.ai |
| TLDR AI | dan@tldr.tech |

## IMAP Details

- Host: imap.gmail.com:993 (SSL). Port 143 STARTTLS blocked from CN.
- Connects read-only, searches last 7 days
- Extracts arxiv.org/abs links and semanticscholar.org URLs
- Source tier: 0.9 (human-curated)
- Max 20 emails per run

## Pitfalls

- **Date format for SEARCH.** IMAP `SEARCH SINCE` requires `"DD-Mon-YYYY"` (e.g. `"12-Jul-2026"`), NOT ISO 8601 or a bare number. Use `datetime.now().strftime("%d-%b-%Y")`. Wrong format causes `BAD Could not parse command`.
- **App Password must be in .env as `GMAIL_APP_PASSWORD`** with exact spacing from Google's display.
- **Label must be "Show in IMAP"** in Gmail Settings → Labels, otherwise IMAP select fails.
