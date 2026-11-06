# Clip Engine: status and handoff

Last updated 2026-11-06. The plain-language state of the product, for the owner, a future buyer, and any Claude session that picks up the work (including the company "Command center" chat).

## What it is

An automated clipping business. Creators in paid clipping programs (for example Whop Content Rewards) pay per 1,000 views for short clips of their videos. Clip Engine finds the creator's newest videos, cuts the best moments into Shorts with word-by-word captions, writes titles, hashtags and #ad disclosure, posts or prepares them for YouTube, TikTok and Instagram, tracks views and estimated earnings, and learns which choices earn the most (Thompson sampling).

- Code: github.com/lilcityholdings-bit/Clip-engine (branch `main`). Tests: `python -m pytest` (125 passing).
- Hosting: Railway project `clip-engine`, service `clip-engine` (always on, about 100 MB idle), volume `clip-engine-data` at `/data`. See README, Keeping Railway costs down. A leftover `wake-clip-engine` cron service from a sleep-mode trial should be deleted.
- Dashboard: https://clip-engine-production-f966.up.railway.app (password = Railway variable `DASHBOARD_PASSWORD`).
- Setup, deploy and platform docs: `README.md`. Secrets and incident procedure: `SECURITY.md`. Accounts: `docs/ASSETS.md`.

## Where things stand

Done
- Engine built, tested and running on Railway. Review mode on (nothing posts without approval); gradual start (4 clips/day for the first week, then 6).
- YouTube: Google Cloud project `clip-engine-512213`, OAuth app published. A test upload on 2026-10-21 was NOT forced private, so no Google audit is needed.
- Platform-rule protections (Oct 2026): own-angle captions, no near-duplicate titles, max 5 specific hashtags, watermarked sources refused, #ad and branded-content labels.
- Security after Railway's 2026-10-21 breach notice: Railway 2FA (passkey), old YouTube login revoked, Google client secret rotated (old one deleted; new one ends in `Ztac`), dashboard password lockout added. Variables redeployed 2026-10-30.

Open, in order
0. Delete the unused `wake-clip-engine` service in Railway (sleep-mode trial; the deletion prompt timed out).
1. Reconnect YouTube on the dashboard (Connect YouTube, then Test a YouTube upload, then Turn on automatic posting). Confirm `DASHBOARD_PASSWORD` was changed after the breach.
2. Claude API key: console.anthropic.com under the company email and a company-named organization; buy $10-20 credit; set a monthly limit (~$25); add as Railway variable `ANTHROPIC_API_KEY`. Nothing is clipped until this exists.
3. First campaign: owner joins a Whop clipping campaign and shares the link; check its rules (AI/automated editing allowed? which platforms pay? required tags?) and add it with Add a campaign. Use the creator's YouTube channel as the source, never TikTok/Instagram links.
4. Make clips now, approve the first clips, paste the post links into Whop daily.
5. Instagram automatic posting: professional account, Meta developer app, the account added as an Instagram Tester, token and user ID into Railway (`INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`). The account was created in the app (Florida blocks web sign-up); confirm it uses the company email.
6. TikTok: post from the phone (dashboard Ready to post). TikTok's Direct Post rules require per-post user choices and only allow private posts until audited, and internal tools for your own accounts aren't an intended use, so automatic TikTok posting is not planned. Later option: apply for the "send to drafts" (video.upload) scope.

## Costs (estimates)

Claude API ~$10-20/month at 4-6 clips/day; Railway: idle memory cut from ~0.8 GB around the clock to ~0.1 GB (Nov 2026), so expect a few dollars a month, likely inside the plan's included usage; platform APIs free. Optional: licensed music subscription.

## Decisions on record

- Content: only campaigns that grant reposting rights, or public-domain/CC archives. No copyrighted trending songs; music only from licensed libraries.
- Ayrshare dropped (cost); phone posting is the free default.
- Accounts belong to the company (lilcityholdings) so the product can be sold separately.
