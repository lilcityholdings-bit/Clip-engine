# Clip Engine

A fully automated clipping business. It finds long videos, cuts the best moments into Shorts, posts them to **YouTube, TikTok and Instagram Reels**, and learns from views and earnings what to make next.

## Easy setup (about 15 minutes, all from a phone)

1. **Ayrshare** (ayrshare.com): sign up on a plan that includes API access and video. Connect your YouTube, TikTok and Instagram accounts in their app, then copy your API key. Ayrshare is already approved by all three platforms, so no developer apps or platform reviews are needed.
2. **Claude API key**: console.anthropic.com → API Keys.
3. **Campaigns**: join 1–3 clipping campaigns (e.g. on Whop) and note each one's pay rate, the creator's channel link, required tags and rules.
4. **Railway**: deploy this repo, add a volume at `/data`, generate a domain, and set only these variables: `ANTHROPIC_API_KEY`, `AYRSHARE_API_KEY`, `PUBLIC_BASE_URL`, `DASHBOARD_PASSWORD`, `CAMPAIGNS_JSON`, `DATA_DIR=/data`.

That's it. The direct YouTube, TikTok and Instagram connections described further down are optional. They save Ayrshare's fee, but each needs a developer app and a platform review.

## How it works

Every 5 minutes the scheduler checks what needs doing:

| When | What |
|---|---|
| Every 3 hours | **Produce:** pick a campaign or topic, get the newest source video, transcribe it, have Claude pick and score the best moments, render them, and queue a post on every platform. |
| Every 5 minutes | **Publish:** post every queued clip whose scheduled time has arrived, within each platform's daily limit. Failed posts retry 3 times, then send an alert. |
| Every hour | **Score:** once a clip has been live for 48 hours, add up its views across platforms and update the strategy. Also refreshes view counts and sends new links to submit. |

### What it learns

Each clip records the choices behind it: **campaign or topic, format, clip length, title style and posting time**. Once the views come in, each choice gets a score. In campaign mode the score is estimated money earned (views × that campaign's pay rate), plus a value for subscribers. The next clip is planned with Thompson sampling, which mostly repeats what earns the most while still testing new options.

### What makes the clips perform

- **Learns from its own results:** once 8 clips have been scored, Claude sees the channel's best and worst clips (real titles, hooks and view counts) every time it picks new moments.
- **Quality filter:** Claude scores every moment from 1 to 10 for watch-through and shares. Anything under 6 is never posted.
- **Speaker crop:** Shorts fill the screen with the speaker's face. When there's no single steady face, they fall back to the full frame over a blurred fill.
- **Word-by-word captions:** the spoken word is highlighted, since most viewers watch on mute.
- **Hook text:** a 2–6 word hook is on screen for the first 3 seconds.
- **Tight edits:** each cut starts on the first spoken word and ends just after the last, so there's no dead air.
- **Loudness:** audio is normalized to −14 LUFS.

## Source modes

- **Campaign mode** (the main one): clips the newest videos of creators who pay per view through clipping programs such as Whop Content Rewards. Joining a program gives you the right to repost that creator's content. You add each campaign to `CAMPAIGNS_JSON`; the format is at the top of `clip_engine/campaigns.py`. Every post credits the creator, includes the campaign's required tags and carries a paid-promotion disclosure: **#ad** in the caption, plus TikTok's branded-content label. The FTC requires this disclosure.
- **Archive mode** (used when no campaigns are set up): clips public-domain films from curated Internet Archive collections (Prelinger, feature films, US government films, NASA). Archive licenses elsewhere are set by uploaders and often wrong, so those aren't used. Accepted licenses are public domain, CC0, CC BY and CC BY-SA.

Whop has no API for submitting clips, so new post links go to `DIGEST_WEBHOOK_URL` (a Discord or Slack channel) and appear on the dashboard, ready to paste. Programs that track views by connecting your accounts need no submission step.

## Dashboard

Open `PUBLIC_BASE_URL` in a browser and sign in with any username and `DASHBOARD_PASSWORD`. It shows:

- estimated earnings and views, per campaign
- links waiting to be submitted, with a "Mark all submitted" button
- what the strategy has learned
- recent clips with per-platform views
- recent errors
- **Pause/Resume** and **Make clips now** buttons

Rendered clips are served at `/media/<random token>.mp4` (Instagram fetches them from there) and deleted once they're posted everywhere.

## Deploy (Railway)

1. Create a Railway project called **clip-engine** and deploy this repo. The `Dockerfile` installs ffmpeg and a JavaScript runtime for yt-dlp.
2. Add a **volume** mounted at `/data`.
3. **Generate a domain** under Settings → Networking, and put it in `PUBLIC_BASE_URL`.
4. Set the variables from `.env.example`. Any platform without credentials is simply skipped.

## One-time platform setup

Use company accounts for all of these so the business can be transferred to a buyer.

**YouTube**
1. Create the channel as a **Brand Account** under the company Google account.
2. In Google Cloud Console: enable **YouTube Data API v3** and **YouTube Analytics API**, set up the OAuth consent screen (External) and **publish** it, then create an OAuth client of type *Desktop app*.
3. Run `python scripts/get_youtube_token.py` and put the three printed values into Railway.
4. Apply for the **YouTube API audit** ("YouTube API Services - Audit and Quota Extension"). Until the project passes, API uploads are locked to private.

**TikTok**
1. On developers.tiktok.com, create an app with the **Content Posting API** (Direct Post), and request the scopes `video.publish` and `video.list`.
2. Authorize your TikTok account once to get a refresh token. Tokens are then refreshed automatically.
3. Submit the app for **audit**. Until it passes, posts are private (`TIKTOK_PRIVACY=SELF_ONLY`).

**Instagram**
1. Switch the Instagram account to a **professional** (Business or Creator) account.
2. On developers.facebook.com, create an app using **Instagram API with Instagram Login**, with `instagram_business_basic` and `instagram_business_content_publish`.
3. Generate a long-lived token and put it and the account's user ID into Railway. The engine refreshes the token weekly.
4. Submit for **App Review** to post on accounts other than the app's testers.

## Run locally

```bash
pip install -r requirements.txt          # also needs ffmpeg installed
cp .env.example .env                     # fill in values, then export them
python -m clip_engine run                # dashboard + scheduler
python -m clip_engine produce            # one production cycle
python -m clip_engine publish            # post whatever is due
python -m clip_engine report             # strategy, earnings, links to submit
python -m pytest                         # tests
```

## Costs

- **Claude API:** 2–3 calls per production cycle, a few cents each.
- **Railway:** one worker with about 2 GB of RAM (Whisper and ffmpeg), plus a 10 GB volume.
- **Platform APIs:** free within their limits.
