# Tweet Thread Scraper

A small desktop app that exports a post, quote posts, and replies to JSON.

## Run from source

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync
uv run python main.py
```

Enter an X username and a tweet URL or ID. The first sign-in also needs the account password, email address, and an email app password with IMAP access enabled so `twscrape` can retrieve verification codes. Later runs can reuse the saved session.

If sign-in returns a Cloudflare block page, it is not a password error. Stop retrying and ask corporate IT whether network policy, proxying, or TLS inspection is involved; if not, contact X support with the Cloudflare Ray ID shown by the app. The app does not alter network settings or attempt to bypass the block.

The app exports `tweet_<id>_data.json` to the selected folder. It fetches up to 500 quote posts and 1,000 replies, subject to what X makes available to the account.

## Build for Windows and macOS

The GitHub Actions workflow builds a native executable for each operating system. Run it from the Actions tab with **Build desktop app**, or push a version tag such as `v0.1.0`. Download the resulting Windows or macOS artifact from the workflow run.

These builds are unsigned. Windows SmartScreen or macOS Gatekeeper may show a first-launch warning. Avoiding those warnings requires platform signing credentials; macOS distribution without a warning also requires notarization.

For a local build on the current operating system:

```bash
uv sync --dev
uv run pyinstaller --noconfirm --clean --onefile --windowed --collect-all twscrape --name TweetThreadScraper main.py
```

The executable is written to `dist/`. Build on Windows for Windows and on macOS for macOS.

## Account data

`twscrape` stores account credentials and session data in a local SQLite database under the operating system's application-data directory (`TweetThreadScraper/accounts.db`). The app does not put credentials in its settings or in the executable. Do not share that database. X login and scraping availability are controlled by X; use an account you are authorized to use and follow the platform's terms.