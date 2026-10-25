# Security runbook

What to protect, and what to do if a hosting account or key is exposed. Keep this current, because a buyer's due diligence reads it.

## Where secrets live

| Secret | Where | Rotate by |
|---|---|---|
| `YOUTUBE_CLIENT_SECRET` | Railway variable | Google Cloud, Google Auth Platform, Clients: add a new secret, delete the old one, update Railway |
| `DASHBOARD_PASSWORD` | Railway variable | Set a new long password in Railway |
| YouTube login token | SQLite on the Railway volume (`/data`) | Dashboard, Settings, **Disconnect and cancel login**, then **Reconnect** |
| `ANTHROPIC_API_KEY` | Railway variable | console.anthropic.com: create a new key, delete the old one, set a spending limit |
| `INSTAGRAM_ACCESS_TOKEN`, `TIKTOK_*`, `AYRSHARE_API_KEY` | Railway variables | Regenerate in each provider's dashboard |
| `DIGEST_WEBHOOK_URL` | Railway variable | Delete and recreate the webhook |

Nothing secret goes into git. `.env.example` lists the names only.

## If the host or an account is compromised

1. Secure the hosting account first: new password, two-factor authentication, sign out other sessions, delete API tokens, review members.
2. Pause the engine (dashboard Pause button) so nothing posts while you rotate.
3. Cancel the YouTube login (Disconnect and cancel login), and remove the app at myaccount.google.com/permissions if the dashboard can't be reached.
4. Rotate every secret in the table above, newest value first, then delete the old one.
5. Redeploy, reconnect YouTube, and resume.
6. Review YouTube Studio, Google account activity and the AI provider's usage page for anything unexpected.
7. Add a row to the incident table in `docs/ASSETS.md`.

## Built-in protections

- The dashboard locks an address out for 15 minutes after 5 wrong passwords.
- Review mode holds every clip until it is approved.
- If Google keeps an automatic upload private, automatic YouTube posting switches itself off.
- Accounts used by the engine should have two-factor authentication, spending caps and no more permissions than needed.
