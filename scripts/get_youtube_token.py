"""One-time setup: sign in to the channel's Google account and print a refresh token.

1. In Google Cloud Console, create an OAuth client of type "Desktop app" and
   download it as client_secret.json into this folder.
2. pip install google-auth-oauthlib
3. python scripts/get_youtube_token.py
4. Put the printed values into Railway variables.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
