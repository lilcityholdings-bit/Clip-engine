# Asset register

Every external account and dependency this product relies on. Keep it current, because a buyer's due diligence starts here.

| Asset | Purpose | Owner account | Transfer method |
|---|---|---|---|
| GitHub repo `clip-engine` | Source code | lilcityholdings-bit | GitHub repo transfer |
| YouTube channel (Brand Account) | Where clips are published | Company Google account (TBD) | Add new primary owner, then remove old owner |
| Google Cloud project | YouTube API credentials and quota | Company Google account (TBD) | IAM: add buyer as Owner |
| Anthropic API key | Picks clips and writes titles | Company Anthropic org (TBD) | Buyer issues their own key |
| Railway project `clip-engine` | Hosting, plus a volume for the database | Company Railway account (TBD) | Railway project transfer |

## Content sources and licenses

- Internet Archive curated collections: `prelinger`, `feature_films`, `fedflix`, `nasa`, `usgovernmentdocuments`.
- Accepted licenses: public domain, CC0, CC BY, CC BY-SA.
- Every upload's source, creator and license is stored in the `sources` table and written in the video description.
