# Clip Engine

A fully automated YouTube clip channel. Every 6 hours it:

1. **Scores** videos that are at least 48 hours old (views and subscribers gained), and feeds the results into its strategy.
2. **Finds trends** by reading YouTube's trending chart and having Claude turn it into search topics.
3. **Plans** the next upload, choosing a format (Short or longer clip), clip length, topic and title style. It uses Thompson sampling, so it mostly repeats what earns views and subscribers while still testing new options.
4. **Sources** a long-form, public-domain or openly licensed video from curated Internet Archive collections.
5. **Clips** it: transcribes with Whisper, has Claude pick the strongest self-contained moments and write titles, then cuts, reformats and captions them with ffmpeg.
6. **Uploads** to YouTube with full credit to the original creator and license. It stays under the daily API quota.

## Why only licensed sources

Reposting other creators' videos gets copyright strikes, and the channel is terminated at three. A channel with strikes is also close to worthless to a buyer. So the engine only uses:

- **Curated collections** (`TRUSTED_COLLECTIONS` in `clip_engine/sources.py`): Prelinger Archives, public-domain feature films, US government films and NASA. Uploader-set licenses elsewhere on archive.org are often wrong, so the engine doesn't use them.
- **Licenses that allow commercial use and edits:** public domain, CC0, CC BY and CC BY-SA. Anything NonCommercial or NoDerivatives is rejected.

Public-domain films sometimes get wrongly claimed by Content ID. Claims aren't strikes, so dispute them in YouTube Studio if they show up.

## Run locally

```bash
pip install -r requirements.txt          # also needs ffmpeg installed
cp .env.example .env                     # fill in values, then export them
python -m clip_engine produce            # one find → clip → upload cycle
python -m clip_engine score              # score old uploads
python -m clip_engine report             # what the strategy has learned
python -m pytest                         # tests
```

## Deploy (Railway)

1. Create a Railway project called **clip-engine** and deploy this repo. The `Dockerfile` installs ffmpeg.
2. Add a **volume** mounted at `/data`, which holds the database and temporary video files.
3. Set the variables from `.env.example`.
4. The service runs `python -m clip_engine run` forever.

## One-time YouTube setup

1. Create the channel as a **Brand Account** under the company Google account. A Brand Account can be transferred to a buyer.
2. In Google Cloud Console (same company account): create a project, enable **YouTube Data API v3** and **YouTube Analytics API**, set up the OAuth consent screen, and create an OAuth client of type *Desktop app*.
3. Run `python scripts/get_youtube_token.py` and sign in as the channel. Copy the three printed values into Railway.
4. **Request an API audit.** Until the Google Cloud project passes YouTube's API compliance audit, videos uploaded through the API are locked to private. Apply using the "YouTube API Services - Audit and Quota Extension" form. The same form raises the upload limit: the default quota allows about 6 uploads a day.

## Costs

- **Claude API:** about 2 calls per cycle, a few cents each.
- **Railway:** one small worker, plus a volume of about 5 GB.
- **YouTube API:** free within quota.
