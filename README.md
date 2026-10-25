# Clip Engine

A fully automated clipping business. It finds long videos, cuts the best moments into Shorts, posts them to **YouTube, TikTok and Instagram Reels**, and learns from views and earnings what to make next.

## Phone posting mode (free, the default)

Any platform without an automatic posting connection is posted by you from your phone. The engine still finds the moments, cuts, frames and captions the clip, writes the title, hashtags, required tags and #ad, and picks a time. Approved clips appear under **Ready to post** on the dashboard, with a **Download video** button, **Copy** buttons for the title and caption, and per-app tips. Post in the app, which also lets you add a trending sound, then paste the post's link into the box. The link joins your Whop links, and its public view count feeds the strategy. Clips nobody posts within 7 days are dropped.

A platform switches to automatic posting as soon as its connection (Ayrshare or a direct API key) is added in Railway.

## Easy setup (about 15 minutes, all from a phone)

1. **Ayrshare** (ayrshare.com): sign up on a plan that includes API access and video. Connect your YouTube, TikTok and Instagram accounts in their app, then copy your API key. Ayrshare is already approved by all three platforms, so no developer apps or platform reviews are needed.
2. **Claude API key**: console.anthropic.com → API Keys.
3. **Campaigns**: join 1–3 clipping campaigns (e.g. on Whop), then add each one on the dashboard with **Add a campaign**: name, campaign link, pay per 1,000 views, the creator's channel links, required caption tags, the creator's rules and which platforms it pays for. Campaigns can be edited, paused or deleted there too.
4. **Railway**: deploy this repo, add a volume at `/data`, generate a domain, and set only these variables: `ANTHROPIC_API_KEY`, `AYRSHARE_API_KEY`, `PUBLIC_BASE_URL`, `DASHBOARD_PASSWORD`, `DATA_DIR=/data`.

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

- **Rides current trends:** every 12 hours, for each campaign's niche, Claude searches the web for what's trending on Shorts, TikTok and Reels (hashtags, phrases, title formats, hot topics) and reads Google's daily trending searches. Titles, hooks and 3–5 hashtags per clip use that language when it honestly fits. The dashboard shows the latest trends.
- **Music:** put tracks the company is **licensed** to use in monetized social videos (for example from an Epidemic Sound or Artlist subscription) in `/data/music`. The engine mixes one under the speech, ducking it whenever someone talks, and learns which tracks, or no music, earn the most. With `TIKTOK_AUTO_MUSIC=1`, clips without a bed get TikTok's own licensed recommended music. Trending songs themselves can't be used: they're copyrighted and get clips muted or claimed, and the posting APIs can't attach in-app sounds. The dashboard lists trending sounds so you can add licensed equivalents. Campaigns that ban added music set `"allow_music": false`.
- **Learns from its own results:** once 8 clips have been scored, Claude sees the channel's best and worst clips (real titles, hooks and view counts) every time it picks new moments.
- **Quality filter:** Claude scores every moment from 1 to 10 for watch-through and shares. Anything under 6 is never posted.
- **Most-replayed moments:** for YouTube videos, the engine reads the "most replayed" graph and transcribes the minutes around the top peaks, anywhere in a 3-hour podcast. Claude is told which lines viewers rewatch most.
- **Loud moments:** the transcript flags lines where the speaker gets louder than usual (laughing, shouting, excitement), since Claude can't hear the audio.
- **Framing:** two-person shows get a stacked layout, with each speaker zoomed to head-and-shoulders in their own half and captions on the seam. Single speakers get a crop that follows their face and jumps with camera cuts. With no clear faces, it falls back to the full frame over a blurred fill.
- **Word-by-word captions:** the spoken word is highlighted, since most viewers watch on mute.
- **Hook text:** a 2–6 word hook is on screen for the first 3 seconds.
- **Tight edits:** each cut starts on the first spoken word and ends just after the last, so there's no dead air.
- **Loudness:** audio is normalized to −14 LUFS.

## Source modes

- **Campaign mode** (the main one): clips the newest videos of creators who pay per view through clipping programs such as Whop Content Rewards. Joining a program gives you the right to repost that creator's content. You add each campaign to `CAMPAIGNS_JSON`; the format is at the top of `clip_engine/campaigns.py`. Every post credits the creator, includes the campaign's required tags and carries a paid-promotion disclosure: **#ad** in the caption, plus TikTok's branded-content label. The FTC requires this disclosure.
- **Archive mode** (used when no campaigns are set up): clips public-domain films from curated Internet Archive collections (Prelinger, feature films, US government films, NASA). Archive licenses elsewhere are set by uploaders and often wrong, so those aren't used. Accepted licenses are public domain, CC0, CC BY and CC BY-SA.

Whop has no API for submitting clips, so new post links go to `DIGEST_WEBHOOK_URL` (a Discord or Slack channel) and appear on the dashboard, ready to paste. Programs that track views by connecting your accounts need no submission step.

## Review mode and gradual start

- **Review mode** is on by default. New clips appear on the dashboard under "Waiting for your approval", with a video preview, the hook, the caption and the platforms. Nothing posts until you tap **Approve**. **Reject** deletes the clip. Turn review mode off under Settings once you trust the output.
- **Gradual start** protects new accounts from looking like spam: up to 4 clips a day in the first week, then `MAX_CLIPS_PER_DAY` (6). It can be switched off under Settings.
- Clip-making runs in the background, so scheduled posts go out on time even during a long transcription. A 10-minute video needs about 0.9 GB of memory and a few minutes of CPU.

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

**YouTube** (works from a phone browser; no computer or script)
1. Create the channel as a **Brand Account** under the company Google account.
2. In Google Cloud Console (console.cloud.google.com, same Google account): create a project, then **APIs & Services → Library** and enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **OAuth consent screen** (Google Auth Platform): choose **External**. For the app's links, use your dashboard address as the home page and `<dashboard address>/privacy` as the privacy policy. Add your Google account as a test user, then **publish the app to production**. While it's in Testing mode, the login token expires every 7 days.
4. **Credentials → Create credentials → OAuth client ID → Web application.** Under *Authorized redirect URIs* add `<dashboard address>/oauth/youtube/callback` exactly.
5. Copy the client ID and secret into Railway Variables as `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`.
6. On the dashboard, under Settings → **YouTube connection**, tap **Connect YouTube**, sign in as the channel's account and allow access. Google warns that the app is unverified (it's your own app); choose Advanced → continue.
7. Tap **Test a YouTube upload** on the dashboard. It uploads a 3-second unlisted video and reports whether Google forced it private. Delete the test video in YouTube Studio afterwards. Google's current documentation says uploads from unverified projects are not restricted to private, so you may not need an audit. If the test says uploads are kept private, apply with the **YouTube API Services - Audit and Quota Extension Form** (https://support.google.com/youtube/contact/yt_api_form). If it passes, tap **Turn automatic posting on**. If Google ever keeps an automatic upload private, the engine switches YouTube back to phone posting and shows a warning.

**TikTok**
1. On developers.tiktok.com, create an app with the **Content Posting API** (Direct Post), and request the scopes `video.publish` and `video.list`.
2. Authorize your TikTok account once to get a refresh token. Tokens are then refreshed automatically.
3. Submit the app for **audit**. Until it passes, posts are private (`TIKTOK_PRIVACY=SELF_ONLY`).

**Instagram**
1. Switch the Instagram account to a **professional** (Business or Creator) account.
2. On developers.facebook.com, create an app using **Instagram API with Instagram Login**, with `instagram_business_basic` and `instagram_business_content_publish`.
3. Generate a long-lived token and put it and the account's user ID into Railway. The engine refreshes the token weekly.
4. Submit for **App Review** to post on accounts other than the app's testers.

## Security

See `SECURITY.md` for where each secret lives, how to rotate it and what to do after an incident. The dashboard's YouTube card has a **Disconnect and cancel login** button.

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
