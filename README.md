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

## Getting started checklist

Until setup is finished, the top of the dashboard shows a checklist (Claude key, Connect YouTube, test upload, automatic posting, first campaign, first approved clip). It names the next step and puts its button right there, so you never have to hunt through Settings.

## Platform rules the engine follows

Checked October 2026; the details are in `clip_engine/policy.py`.

- **Original angle:** every caption opens with the channel's own take (why the moment matters, context, or a question), not just a summary. YouTube, Instagram and TikTok all demote plain reposts.
- **No look-alike uploads:** Claude sees the last 30 days of titles, and any new clip whose title nearly repeats one is dropped. Near-identical uploads count as mass-produced content on YouTube.
- **Hashtags:** at most 5 (Instagram's cap since December 2025), all specific to the clip. Generic tags such as #fyp, #viral, #reels and #explore are removed.
- **No watermarks:** campaign sources from TikTok, Instagram or Facebook are refused. Their videos carry the app's logo, which TikTok and Instagram treat as unoriginal.
- **Disclosure:** campaign clips carry #ad and TikTok's branded-content setting. YouTube uploads state that the video contains no AI-generated or altered footage.
- **Pace:** review mode, the gradual start and daily caps keep new accounts from looking like spam.

What the engine can't do for you: Instagram leaves accounts that **mostly** repost others' work out of recommendations, so post some original content of your own on each account as well. On YouTube, campaign clips are uploaded with **Includes paid promotion** switched on (`paidProductPlacementDetails`). If YouTube doesn't confirm the label for a Short, the dashboard lists it with a link to its Studio page so you can tick it by hand.

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
- **Archive mode** (off by default; set `ARCHIVE_MODE=1` to use it when no campaign is live): clips public-domain films from curated Internet Archive collections (Prelinger, feature films, US government films, NASA). Archive licenses elsewhere are set by uploaders and often wrong, so those aren't used. Accepted licenses are public domain, CC0, CC BY and CC BY-SA.

Whop has no API for submitting clips, so new post links go to `DIGEST_WEBHOOK_URL` (a Discord or Slack channel) and appear on the dashboard, ready to paste. Programs that track views by connecting your accounts need no submission step.

## Review mode and gradual start

- **Review mode** is on by default. New clips appear on the dashboard under "Waiting for your approval", with a video preview, the hook, the caption and a tick box for each account. Untick any account you don't want that clip on, then tap **Approve**; nothing posts until you do.
- **Post new clips to** (Settings) sets which accounts every new clip is made for. A campaign's own platform list still applies, so a clip only goes where the campaign pays and you've ticked. **Reject** deletes the clip. Turn review mode off under Settings once you trust the output.
- **Gradual start** protects new accounts from looking like spam: up to 4 clips a day in the first week, then `MAX_CLIPS_PER_DAY` (6). It can be switched off under Settings.
- Clip-making runs in the background, so scheduled posts go out on time even during a long transcription. A 10-minute video needs about 0.9 GB of memory and a few minutes of CPU.

## Videos a campaign gives you

Some campaigns hand clippers a source video to download (a Drive or Dropbox file, a download button) instead of a channel to watch. On the dashboard, under **Add your own video**, pick the campaign and either **upload the file from your phone** (up to `MAX_UPLOAD_MB`, default 2000) or **paste its link**. Your video is clipped before anything the engine finds on its own, credited to the creator name you give (or the campaign), with the campaign's tags and #ad. Clip-making starts by itself as soon as the upload or link is in (resuming the engine if it was paused); there is nothing else to tap. Uploaded files are deleted once their clips are made. Links go through yt-dlp, so YouTube, public Drive and Dropbox links work; private links don't, so upload those instead.

**Several videos and photos, made into one or more new videos:** under **Add your own video** you can pick many files at once, videos and photos (`.jpg`, `.png`, `.webp`), choose **Combine them into new videos**, say **How many different videos** you want (1 to 4) and **how long each one should be** (15 to 30 seconds, or up to 59). Claude looks at one frame of each file (and, for a single combined video with no Title typed, also writes its caption), splits them into that many different videos (each file used in at most one), puts each in the order that tells the clearest story (strongest shot first), writes each a short truthful caption describing what is shown, and leaves out clearly unusable files (blurry, near-duplicate, another app's watermark). If Claude isn't available the files are split evenly in the order picked and captioned "<Title> 1", "<Title> 2". Each video is 1080x1920: every piece is fitted over a blurred copy of itself, photos get a slow zoom (3 seconds each), videos keep their whole length and their own sound, and the pieces are cut together back to back. If a video's material adds up to more than the chosen length, each piece is shortened from its start in proportion, and the last pieces are dropped if that is still not enough; with too little for 15 seconds, photos are held longer (up to 5 seconds each) and a very short set simply stays short. So give the engine enough files: about 6 to 10 photos or short clips per video. Each finished video is posted whole as its own clip, with the campaign's approved caption if it has one (in turn), otherwise Claude's caption (or the Title you typed when there is one video), with the campaign's tags and #ad or its disclosure line. Never more than 4 videos per upload, and never more clips than today's limit allows. The only Claude call is the planning one (one small image per file). Only use photos and footage the campaign supplies or the company owns. Up to 30 files per upload; an upload that never finishes is cleaned up after 24 hours. Choose **Clip each video separately** to cut each video into clips as before (videos only).

**Phone layout:** the dashboard puts what you use every day first (the to-do bar, Pause and Make clips now, Your clips, clips to approve, Ready to post, Posting automatically, Links to submit, Add your own video, Campaigns) and folds the rest (earnings and stats, trends, what's working, recent clips, problems, Settings) under **More**. Shortcuts to a folded section open it.

**Submit deadlines and the posting checklist:** a campaign can say how soon a link must be submitted after posting (**Submit within**, minutes) and how long posts must stay up (**Keep posts up for**, days). Each link under **Links to submit** then shows **Submit by 2:45pm ET, 12 min left** (or that the window closed, with a pointer to Post again), a Copy link button, an Open campaign page button and its own Mark submitted. The to-do bar shows the first deadline, and saving a post's link tells you its deadline straight away. Every clip under Ready to post, and on its own page, shows a **Before you post** checklist built from the campaign's rules (disclosure line, no added sound, no paid-partnership switch or boosting, visible likes, the submit window, how long to keep it up).

**Your clips:** the dashboard has a **Your clips** section listing every clip from the last 14 days that is still waiting, approved or posted, grouped by campaign. Tap **Open** on a clip for its own page: the video player, the caption, where it goes and its status, and the buttons that fit its state (**Approve** with the YouTube time, **Reject**, **Delete**, **Download**, **Post again**, **Delete video**). While clips are being made the section says so.

**Uploads survive restarts and crashes:** the video being clipped is also kept as "in flight"; if the server restarts mid-run (a deploy) or the clip-making process dies (for example out of memory), it goes back to the front of the queue on start-up or at the next 5-minute check and is clipped again, up to 3 times, and its files are then deleted. A single video chosen with **Combine** is cut into clips instead, since there is nothing to combine it with. Don't deploy while a run is going: check the logs first.

**Watching, approving, downloading and deleting clips:** each upload has a **Let me watch each clip before it can post** tick box (on by default), so its clips wait under **Waiting for your approval** whatever the global Review setting says. Every clip there has a video player, **Download**, **Approve**, **Reject** and **Delete** (both remove it). Approved clips can still be downloaded and **Deleted** before they post, under **Ready to post** (TikTok, Instagram) and **Posting automatically** (YouTube). Delete never touches a post that already went out. A clip that was posted and is kept for reposting has **Delete video** under **Post a clip again**, which removes its saved video for good (the record of what was posted stays).

**Posting a clip again:** only clips that actually went out are listed, and every Post again button asks for confirmation first (on YouTube the clip goes up publicly within 5 minutes). A posted campaign clip's video is kept for 14 days. The dashboard's **Post a clip again** list has a **Post again on TikTok / Instagram / YouTube** button for each; it puts the clip back under **Ready to post** (or, for YouTube, posts it within 5 minutes). Use it when a campaign's submit window was missed (some only take a post submitted within 30 minutes, so post, copy the link, submit it on the campaign page first, then save the link in the dashboard) or a post was lost. Leave the earlier post up, since campaigns can reject removed posts. Each clip can be posted at most 5 times in total. Clips that were rejected, skipped or never posted are deleted at once. Older databases are migrated automatically on start-up (the posts table no longer allows only one post per clip and account; only one *open* post at a time).

**Skipping rating cards and logos:** videos like game trailers open with a rating card and close with logos, which make a bad first or last second. In **Skip at the start** and **Skip at the end** (when adding or editing a campaign) put the seconds to cut from every source video. Clips never start before the skipped start or run past the skipped end, and a clip that would be left under 5 seconds is dropped. It doesn't apply to videos you build from several files, which you control yourself.

**Campaigns that want the disclosure on its own line** (for example "the first hashtag, alone on the line right after the caption, then up to 3 more"): type that hashtag (`#Ad`) in **Disclosure on its own line** when adding the campaign. Each post's caption is then exactly: the clip's caption text (no tags or mentions of its own), a line with only `#Ad`, and a line with at most 3 related hashtags. No credit line, link or `#Shorts` is added. Put the campaign's wording rules (for example "no made-up facts, stats or dates; don't copy other clippers' captions") in **Creator's rules** so Claude follows them when writing the caption.

**Campaigns that ban paid promotion or added sound:** untick **Use the apps' own paid-promotion switch** when adding the campaign if its rules say "paid promotion or ads of any kind" is a ban (the caption's `#Ad` is then the disclosure). YouTube uploads are then not marked as paid promotion, and the hand-posting tips for TikTok and Instagram tell you not to switch on Branded content / Paid partnership or boost the post. Untick **Background music allowed** and the tips also tell you not to add a sound. From any one uploaded video the engine makes the number of clips you ask for (**How many clips do you want?**, 1 to 4, on the upload and link forms; 2 if you don't choose). Claude is told to return that many distinct moments, taking the next best ones if only one stands out, and fewer are made if the video doesn't have that many different moments, never more than 4 (Claude is told to return at least 2 distinct moments, taking the next best ones if only one stands out; never more than 4, because campaigns often treat the same footage posted more than 5 times as spam). Every clip must be **different footage**: the engine never makes two clips that share more than half a second of the video (Claude is told so, and the code enforces it). The clips of one run are scheduled **3 hours apart** through the day, never between 11pm and 8am Eastern, so there is something to post all day: the first at the clip's best time, the rest after it. The dashboard shows each clip's time under Ready to post and Posting automatically. **You choose the YouTube time:** the approval card and the Posting automatically list have a date-time box (Eastern time) for each YouTube clip, with **Set time**; a time in the past means now, and the engine posts within 5 minutes of the time you pick (it checks every 5 minutes).

**Silent or speechless footage:** when a video has no speech to read (a product reel, b-roll) and the campaign has approved captions, the engine cuts it along the shot changes instead: back-to-back clips of 10-58 seconds that end on a cut, taken evenly across the video, framed vertically, with no burned-in words or hook text, and no Claude call. Without approved captions, a speechless video is skipped because there is no caption to write.

## Phone alerts

Set `DIGEST_WEBHOOK_URL` to a Discord channel webhook (free; the Discord app gives push notifications). Within 5 minutes the engine messages you once when new clips are waiting for approval, and once when a clip you post by hand (TikTok, or Instagram until it's connected) reaches its posting time. Once an hour it also sends the new post links to paste into Whop. Use a Discord server owned by the company so it can be handed over.

## Making clips, and knowing what happened

**Make clips now** starts a run within about 10 seconds (and resumes the engine if it was paused). A status line under the button says what is happening: making clips, how many were made, or the plain reason none were (no new video, today's limit reached, no speech and no approved captions). While a run is going the page refreshes itself so new clips appear at the top. A video you upload gets all of today's remaining clips, and if a run fails part-way your upload is kept and retried (up to 3 times).

## Keeping Railway costs down

Railway bills memory and CPU by the minute, so the always-on part is kept small:

- Clips are made in a **separate process** (`python -m clip_engine produce`), so Whisper's memory (up to 3 GB) is released when it finishes. The always-on part (dashboard and scheduler) uses about 100 MB, down from about 800 MB.
- Without `ANTHROPIC_API_KEY`, no clips are made at all, so nothing is downloaded or transcribed for nothing.
- The heavy clip-making process is only started when there is something to clip: a video you added, or a live campaign with a channel to watch, and room under today's limit. With no campaign the engine idles instead of clipping archive films.
- Videos with no audio track skip transcription, and speechless footage is cut without a Claude call.
- Railway's sleep mode was tried in November 2026 and turned off: the hourly cron wake-up didn't reliably wake the service, which risked missed posts, and with the service idling at 100 MB it would have saved well under $1 a month. The engine still supports it (it keeps itself awake while making clips) if a reliable wake-up is added later.

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
