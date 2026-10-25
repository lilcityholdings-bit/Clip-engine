# Asset register

Every external account and dependency this product relies on. Keep it current, because a buyer's due diligence starts here.

| Asset | Purpose | Owner account | Transfer method |
|---|---|---|---|
| GitHub repo `clip-engine` | Source code | lilcityholdings-bit | GitHub repo transfer |
| YouTube channel (Brand Account) | Where clips are published | Company Google account (TBD) | Add new primary owner, then remove old owner |
| Google Cloud project | YouTube API credentials and quota | Company Google account (TBD) | IAM: add buyer as Owner |
| Anthropic API key | Picks clips and writes titles | Company Anthropic org (TBD) | Buyer issues their own key |
| TikTok account + TikTok developer app | Posting clips (Content Posting API) | Company account (TBD) | Transfer account login; buyer re-authorizes the app |
| Instagram professional account + Meta app | Posting Reels | Company Meta Business account (TBD) | Transfer via Meta Business Suite |
| Ayrshare account | Posting to YouTube, TikTok and Instagram without developer apps | Company email (TBD) | Transfer account; buyer reconnects social accounts |
| Discord/Slack webhook | Links to submit and failure alerts | Company workspace (TBD) | Buyer creates their own webhook |
| Clipping program memberships (e.g. Whop) | Paid campaigns and payouts | Company account (TBD) | Buyer rejoins programs; payouts to company bank account |
| Railway project `clip-engine` | Hosting, plus a volume for the database | Company Railway account (TBD) | Railway project transfer |

## Content sources and licenses

- Background music: only tracks licensed for monetized social use, stored in `/data/music`. Record each track's license source here.

- Internet Archive curated collections: `prelinger`, `feature_films`, `fedflix`, `nasa`, `usgovernmentdocuments`.
- Accepted licenses: public domain, CC0, CC BY, CC BY-SA.
- Every upload's source, creator and license is stored in the `sources` table and written in the video description.

## Security incidents

| Date | What happened | What was done |
|---|---|---|
| 2026-10-21 | Railway disclosed that an unauthorized party may have read environment variables stored on its platform (status.railway.com, security notice of Oct 21). Builds were paused and all Railway API tokens invalidated. | Treated every secret stored in Railway as exposed: Google OAuth client secret, dashboard password and the saved YouTube login. Procedure in `SECURITY.md`. Record the completion date of each rotation below. |

Rotation log (fill in the date when done): Railway password + 2FA: ____ · GitHub reconnected: ____ · Google client secret: ____ · Dashboard password: ____ · YouTube login cancelled and reconnected: ____
