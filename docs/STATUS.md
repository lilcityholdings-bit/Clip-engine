# Clip Engine: status and handoff

Last updated 2026-11-16. The plain-language state of the product, for the owner, a future buyer, and any Claude session that picks up the work (including the company "Command center" chat).

## What it is

An automated clipping business. Creators in paid clipping programs (for example Whop Content Rewards) pay per 1,000 views for short clips of their videos. Clip Engine finds the creator's newest videos, cuts the best moments into Shorts with word-by-word captions, writes titles, hashtags and #ad disclosure, posts or prepares them for YouTube, TikTok and Instagram, tracks views and estimated earnings, and learns which choices earn the most (Thompson sampling).

- Code: github.com/lilcityholdings-bit/Clip-engine (branch `main`). Tests: `python -m pytest` (178 passing).
- Hosting: Railway project `clip-engine`, service `clip-engine` (always on, about 100 MB idle), volume `clip-engine-data` at `/data`. See README, Keeping Railway costs down. A leftover `wake-clip-engine` cron service from a sleep-mode trial should be deleted.
- Dashboard: https://clip-engine-production-f966.up.railway.app (password = Railway variable `DASHBOARD_PASSWORD`).
- Setup, deploy and platform docs: `README.md`. Secrets and incident procedure: `SECURITY.md`. Accounts: `docs/ASSETS.md`.

## Where things stand

Done
- Engine built, tested (178 passing) and running on Railway, always on at about 80 MB idle. Review mode on (nothing posts without approval); gradual start (4 clips/day in week 1, then 6). Currently PAUSED on purpose until the first campaign is added.
- YouTube: channel @littlecityclips connected, test upload passed (not forced private), automatic posting on. No Google audit needed.
- Claude API: Anthropic organization "Little City Digital LLC" (Console), $25 monthly spend limit, key added in Railway as ANTHROPIC_API_KEY. Engine uses claude-opus-5-5.
- Security after Railway's 2026-10-21 breach: rotated the Google client secret and dashboard password, cancelled and reconnected the YouTube login, Railway passkey 2FA. Disconnect-YouTube button and password lockout added.
- Platform-rule protections, per-clip account tick boxes (and default accounts in Settings), phone alerts via a Discord webhook (code done, webhook not set up yet).
- TikTok @littlecityclipz and an Instagram account created (professional/Creator), both under ceorob321@gmail.com; practice posts made on YouTube and TikTok.

Campaigns
- Ring - Drone Launch Clipping: added on the dashboard (approved captions).
- SEGA - Metaphor ReFantazio Trailer Clipping (gaming niche focus from 2026-11-16): set in the Railway variable CAMPAIGNS_JSON (id sega-metaphor), not on the dashboard, so the dashboard can't delete it; edit the variable to change it. Pays on YouTube Shorts, TikTok and Instagram: up to $2.00 / up to $2.00 / $1.50 per 1,000 views, payout $2 min and $800 max per platform; views from Pakistan, India and Bangladesh aren't paid, and the audience must be at least 50% USA. Captions: text, then #Ad alone on the next line, then at most 3 related hashtags (Disclosure on its own line). Source videos: the Heismay Character Trailer (Frame.io link, download and upload it); the Launch Trailer link is still to be added by the campaign. Rules reviewed 2026-11-16: posts must stay live 30 days (never delete them), likes must be visible, 0.50% minimum engagement (5 likes per 1,000 views), no shipping or romance, no violence, no exaggerated or rage-bait hooks, no made-up facts, call it a trailer (never gameplay), original audio must stay audible, no AI voices, no paid promotion or ads of any kind (permanent ban; so paid_label is false for this campaign: no YouTube paid-promotion switch, no TikTok/Instagram branded-content switch, no boosting), same video at most 5 posts. If unsure, ask the Clipping Culture team, not SEGA.

Open, in order
0. Campaigns with approved captions (Ring - Drone Launch, from 2026-11-10): Approved captions box added 2026-11-11. Silent footage is cut by shot changes (added 2026-11-11). Since 2026-11-15 the YouTube paid-promotion label is requested on upload (paidProductPlacementDetails); not yet confirmed on a real upload. Shorts YouTube doesn't confirm are listed on the dashboard to tick by hand.
0. Campaigns that supply a download: use the dashboard's Add your own video (upload or link); added 2026-11-11, untested with a real phone upload. Since 2026-11-13 it takes several videos and photos at once and can make them into one Short (Claude orders them since 2026-11-15) (see README, Videos a campaign gives you); also untested with a real phone upload. The dashboard now has a Posting automatically list (YouTube posts with their time and a Post now button).
1. Join 1-3 clipping campaigns on Content Rewards (contentrewards.com, Whop's clipping product), pay on YouTube Shorts and TikTok, long talking source videos on YouTube, no ban on AI-assisted editing. Add each with Add a campaign (creator's YouTube channel as the source, never TikTok/Instagram links).
2. Tap Resume, then Make clips now; approve the first clips. YouTube posts itself; TikTok is posted by hand from Ready to post.
3. Discord alerts: create a company Discord server and webhook, add DIGEST_WEBHOOK_URL in Railway.
4. Warm up the accounts (daily light use, a few original posts).
5. Instagram automatic posting (Meta developer app, Instagram Tester, token; paused, optional). TikTok stays manual: TikTok's Direct Post rules don't allow unattended posting.
6. Fix the YouTube channel name typo ("Little city cips" to "Little City Clips") on 2026-11-20; replace the profile picture with an original logo.

## Costs (estimates)

Claude API ~$10-20/month at 4-6 clips/day; Railway: idle memory cut from ~0.8 GB around the clock to ~0.1 GB (Nov 2026), so expect a few dollars a month, likely inside the plan's included usage; platform APIs free. Optional: licensed music subscription.

## Decisions on record

- Content: only campaigns that grant reposting rights, or public-domain/CC archives. No copyrighted trending songs; music only from licensed libraries.
- Ayrshare dropped (cost); phone posting is the free default.
- Accounts belong to the company (lilcityholdings) so the product can be sold separately.
