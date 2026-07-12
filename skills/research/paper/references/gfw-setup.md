# GFW Network Setup for Paper Pipeline

When running Hermes from China, some sources are blocked.

## Source Connectivity

| Source | Status | Notes |
|--------|--------|-------|
| GitHub Trending | Direct | Reachable without proxy |
| arXiv API | Direct | export.arxiv.org reachable |
| Tavily API | Direct | api.tavily.com reachable |
| Gmail IMAP | Port 993 | imap.gmail.com:993 works |
| Semantic Scholar | Rate limited | API reachable, aggressive rate limiting |
| Hugging Face | Blocked | Use _fetch_via_proxy() |
| AlphaXiv | Blocked | Use _fetch_via_proxy() |

## Mihomo Proxy

Standard at http://127.0.0.1:7890. In fetch scripts:

```python
from urllib.request import ProxyHandler, build_opener
handler = ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
opener = build_opener(handler)
resp = opener.open(Request(url), timeout=20)
```

Only use proxy for HF and AlphaXiv. Direct sources should NOT go through proxy.

## Gmail IMAP Newsletter

Port 993 reachable. Setup:
1. App Password from myaccount.google.com/apppasswords
2. `.env`: GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
3. Create ResearchFeeds label in Gmail
4. Auto-filter newsletters to that label

IMAP date format is critical:
```python
_since = datetime.now(timezone.utc).strftime("%d-%b-%Y")
mail.search(None, f"(SINCE {_since})")
```
"SINCE 7" or ISO dates will FAIL. Must be "12-Jul-2026".

## Keyword Pitfalls

Generic keywords cause false positives. Use multi-word phrases, add exclude lists, test keywords against existing candidates.

## WeChat iLink Delivery

WeChat iLink bot has quirks:
- Markdown NOT supported — no bold, italic, emoji, or horizontal rules. Plain text only.
- Rate limited: ~30s cooldown between sends. Cron jobs that fire multiple messages in quick succession may be silently dropped.
- If delivery fails with "iLink sendmessage rate limited", wait 30s and retry. The message is queued but not lost — resend.
- The home channel is configured via WEIXIN_HOME_CHANNEL in .env as the account ID. Cron deliver=weixin sends to this channel.
