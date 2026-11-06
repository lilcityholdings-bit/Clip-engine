# Asset register

Every external account and dependency this product relies on. Keep it current, because a buyer's due diligence starts here.

| Asset | Purpose | Owner account | Transfer method |
|---|---|---|---|
| GitHub repo `clip-engine` | Source code | lilcityholdings-bit | GitHub repo transfer |
| YouTube channel @littlecityclips ("Little City Clips") | Where clips are published | littlecitydigital@gmail.com; still to move into a Brand Account | Brand Account: add buyer as primary owner, then remove old owner |
| Google Cloud project `clip-engine-512213` | YouTube API OAuth client (secret rotated 2026-10-30) | Company Google account (verify: littlecitydigital@gmail.com) | IAM: add buyer as Owner |
| Anthropic API key | Picks clips and writes titles | Company Anthropic org (TBD) | Buyer issues their own key |
| TikTok account | Clips posted from the phone (no developer app; see STATUS) | ceorob321@gmail.com (owner's email; recommended to move to littlecitydigital@gmail.com) | Change the account email to the buyer's, hand over the login |
| Instagram professional account + Meta app | Posting Reels | ceorob321@gmail.com (owner's email; recommended to move to littlecitydigital@gmail.com); Meta app not created yet | Change the account email to the buyer's; transfer via Meta Business Suite |
| Ayrshare account | Posting to YouTube, TikTok and Instagram without developer apps | Company email (TBD) | Transfer account; buyer reconnects social accounts |
| Discord/Slack webhook | Links to submit and failure alerts | Company workspace (TBD) | Buyer creates their own webhook |
| Clipping program memberships (e.g. Whop) | Paid campaigns and payouts | Company account (TBD) | Buyer rejoins programs; payouts to company bank account |
| Railway project `clip-engine` | Hosting, plus a volume for the database | lilcityholdings@gmail.com (passkey 2FA) | Railway project transfer |

## Business emails

- lilcityholdings@gmail.com: Railway and the GitHub organization.
- littlecitydigital@gmail.com: the Clip Engine business email (YouTube channel, Google Cloud, password manager). New Clip Engine accounts (Whop, Anthropic) should use it.
- ceorob321@gmail.com: the owner's email; currently on TikTok and Instagram.

Passwords live in Google Password Manager under littlecitydigital@gmail.com, never in this repo.

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
