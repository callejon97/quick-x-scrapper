import os
import sys

# Safeguard against PyInstaller's --noconsole setting sys.stderr/sys.stdout to None
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
    
import asyncio
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable
from urllib.parse import urlsplit

os.environ.setdefault("TWS_TELEMETRY", "0")

from loguru import logger
from twscrape import API, gather

logger.remove()

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def parse_tweet_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return value

    parsed = urlsplit(value)
    host = parsed.netloc.casefold().removeprefix("www.")
    if parsed.scheme in {"http", "https"} and host in {"x.com", "twitter.com", "mobile.twitter.com"}:
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) >= 3 and parts[-2].casefold() == "status" and parts[-1].isdigit():
            return parts[-1]

    raise ValueError("Enter a numeric tweet ID or an X/Twitter status URL.")


def user_data_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "TweetThreadScraper"


def format_tweet(tweet) -> dict:
    return {
        "id": str(tweet.id),
        "created_at": str(tweet.date),
        "author": tweet.user.username,
        "author_name": tweet.user.displayname,
        "text": tweet.rawContent,
        "likes": tweet.likeCount,
        "retweets": tweet.retweetCount,
        "replies_count": tweet.replyCount,
        "quotes_count": tweet.quoteCount,
        "views": tweet.viewCount,
        "url": tweet.url,
    }


def build_result(source_tweet, quotes: list, replies: list) -> dict:
    def by_engagement(tweet: dict) -> tuple:
        return tweet["likes"] + tweet["retweets"], tweet["likes"], tweet["retweets"]

    return {
        "source_tweet": format_tweet(source_tweet),
        "metrics": {
            "total_quotes_found": len(quotes),
            "total_replies_found": len(replies),
        },
        "quotes": sorted((format_tweet(tweet) for tweet in quotes), key=by_engagement, reverse=True),
        "replies": sorted((format_tweet(tweet) for tweet in replies), key=by_engagement, reverse=True),
    }


def fetch_cookies_via_browser(status_callback: Callable[[str], None]) -> tuple[str, str]:
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is required for automatic browser login. Please run:\n"
            "pip install playwright && playwright install chromium"
        )

    status_callback("Launching browser for manual login...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto("https://x.com/login")

        status_callback("Please log in to X in the opened browser window...")
        
        auth_token = None
        ct0 = None
        username = None

        # Wait up to 5 minutes for successful login
        for _ in range(300):
            cookies = context.cookies("https://x.com")
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            auth_token = cookie_dict.get("auth_token")
            ct0 = cookie_dict.get("ct0")
            
            if auth_token and ct0:
                # Try to extract the username from local storage or URL / page state if possible, or fallback
                try:
                    # Look for username in cookies or page elements if available
                    pass
                except Exception:
                    pass
                break
            import time
            time.sleep(1)

        browser.close()

        if not auth_token or not ct0:
            raise RuntimeError("Login timed out or session cookies were not captured.")

        cookie_string = f"auth_token={auth_token}; ct0={ct0}"
        return cookie_string


async def ensure_account(
    api: API,
    username: str,
    cookie_string: str,
    status: Callable[[str], None],
) -> None:
    accounts = await api.pool.accounts_info()
    existing = next((account for account in accounts if account["username"].casefold() == username.casefold()), None)
    if existing and existing["active"]:
        status(f"Using saved session for @{username}.")
        return

    if not cookie_string.strip():
        raise ValueError("Browser cookies are required. Use 'Log in via Browser' to capture them.")

    status("Saving session cookies...")
    if existing:
        await api.pool.delete_accounts(username)
    
    await api.pool.add_account_cookies(username, cookie_string)
    status("Session cookies registered successfully.")


async def scrape_and_save(api: API, tweet_id: str, output_file: Path, status: Callable[[str], None]) -> Path:
    status("Looking up the original post...")
    source_tweet = await api.tweet_details(int(tweet_id))
    if not source_tweet:
        raise ValueError("That post could not be found or is not available to this account.")

    status(f"Fetching quote posts for @{source_tweet.user.username}...")
    quotes = await gather(api.search(f"quoted_tweet_id:{tweet_id}", limit=500))

    status("Fetching replies...")
    replies = await gather(
        api.search(f"to:{source_tweet.user.username} in_reply_to_tweet_id:{tweet_id}", limit=1000)
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(build_result(source_tweet, quotes, replies), indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_file


class ScraperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self.output_directory = tk.StringVar(value=str(Path.home() / "Documents" / "TweetThreadScraper"))
        self.username = tk.StringVar()
        self.cookies = tk.StringVar()
        self.tweet = tk.StringVar()

        root.title("Tweet Thread Scraper")
        root.geometry("620x680")
        root.minsize(560, 600)
        self._configure_style()
        self._build_interface()
        self.root.after(100, self._process_events)

    def _configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Title.TLabel", background="#f4f6f8", foreground="#18232e", font=("TkDefaultFont", 19, "bold"))
        style.configure("Subtitle.TLabel", background="#f4f6f8", foreground="#53616f", font=("TkDefaultFont", 10))
        style.configure("Card.TLabelframe", background="#ffffff", padding=14)
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#18232e", font=("TkDefaultFont", 10, "bold"))
        style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"), padding=(14, 9))
        style.configure("Secondary.TButton", font=("TkDefaultFont", 10), padding=(10, 6))
        style.configure("Status.TLabel", background="#f4f6f8", foreground="#53616f")

    def _build_interface(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(28, 24))
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Tweet Thread Scraper", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            shell,
            text="Export a post, its quote posts, and replies to a JSON file.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 18))

        account = ttk.LabelFrame(shell, text="X account session", style="Card.TLabelframe", labelanchor="nw")
        account.pack(fill="x", pady=(0, 14))
        account.columnconfigure(1, weight=1)
        
        self._field(account, "Username", self.username, 0)
        self._field(account, "Cookies string", self.cookies, 1, show="*")

        btn_frame = ttk.Frame(account, style="App.TFrame")
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        
        login_btn = ttk.Button(btn_frame, text="Log in via Browser & Fetch Cookies", style="Secondary.TButton", command=self._browser_login)
        login_btn.pack(anchor="w")

        ttk.Label(
            account,
            text="Click the button above to launch a browser, log in manually, and automatically grab your session cookies.",
            wraplength=500,
            justify="left",
            background="#ffffff",
            foreground="#53616f",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        target = ttk.LabelFrame(shell, text="Post to scrape", style="Card.TLabelframe", labelanchor="nw")
        target.pack(fill="x", pady=(0, 14))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="Tweet URL or ID").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Entry(target, textvariable=self.tweet).grid(row=0, column=1, sticky="ew", pady=5)

        destination = ttk.LabelFrame(shell, text="Save results", style="Card.TLabelframe", labelanchor="nw")
        destination.pack(fill="x", pady=(0, 18))
        destination.columnconfigure(0, weight=1)
        ttk.Entry(destination, textvariable=self.output_directory).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(destination, text="Browse...", command=self._choose_directory).grid(row=0, column=1)

        self.start_button = ttk.Button(shell, text="Start scrape", style="Primary.TButton", command=self._start)
        self.start_button.pack(anchor="e")
        self.progress = ttk.Progressbar(shell, mode="indeterminate")
        self.progress.pack(fill="x", pady=(16, 8))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(shell, textvariable=self.status, style="Status.TLabel", wraplength=550).pack(anchor="w")

    @staticmethod
    def _field(parent, label: str, variable: tk.StringVar, row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(parent, textvariable=variable, show=show or "").grid(row=row, column=1, sticky="ew", pady=4)

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_directory.get() or str(Path.home()))
        if selected:
            self.output_directory.set(selected)

    def _browser_login(self) -> None:
        if self.running:
            return
        self.running = True
        self.start_button.state(["disabled"])
        self.progress.start(10)
        self.status.set("Opening browser for login...")

        def run_browser_thread():
            try:
                cookie_str = fetch_cookies_via_browser(lambda msg: self.events.put(("status", msg)))
                self.events.put(("cookies_fetched", cookie_str))
            except Exception as error:
                self.events.put(("error", str(error) or error.__class__.__name__))

        threading.Thread(target=run_browser_thread, daemon=True).start()

    def _start(self) -> None:
        if self.running:
            return
        try:
            tweet_id = parse_tweet_id(self.tweet.get())
        except ValueError as error:
            messagebox.showerror("Check the post", str(error), parent=self.root)
            return
        username = self.username.get().strip().lstrip("@")
        if not username:
            messagebox.showerror("Username required", "Enter your X username.", parent=self.root)
            return
        cookie_string = self.cookies.get().strip()
        if not cookie_string:
            messagebox.showerror("Cookies required", "Click 'Log in via Browser' to capture your session cookies first.", parent=self.root)
            return
        directory_text = self.output_directory.get().strip()
        if not directory_text:
            messagebox.showerror("Save location required", "Choose a folder for the JSON file.", parent=self.root)
            return
        output_directory = Path(directory_text).expanduser()

        self.running = True
        self.start_button.state(["disabled"])
        self.progress.start(10)
        self.status.set("Preparing scrape...")
        credentials = (username, cookie_string)
        threading.Thread(
            target=self._run_job,
            args=(tweet_id, credentials, output_directory),
            daemon=True,
        ).start()

    def _run_job(self, tweet_id: str, credentials: tuple[str, str], output_directory: Path) -> None:
        username, cookie_string = credentials
        try:
            app_data = user_data_directory()
            app_data.mkdir(parents=True, exist_ok=True)
            api = API(
                str(app_data / "accounts.db"),
                raise_when_no_account=True,
                wait_timeout=60,
            )

            async def run() -> Path:
                def report_status(message: str) -> None:
                    self.events.put(("status", message))

                await ensure_account(api, username, cookie_string, report_status)
                destination = output_directory / f"tweet_{tweet_id}_data.json"
                return await scrape_and_save(api, tweet_id, destination, report_status)

            output_file = asyncio.run(run())
        except Exception as error:
            self.events.put(("error", str(error) or error.__class__.__name__))
        else:
            self.events.put(("done", str(output_file)))

    def _process_events(self) -> None:
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            
            if event == "status":
                self.status.set(value)
            elif event == "cookies_fetched":
                self.running = False
                self.start_button.state(["!disabled"])
                self.progress.stop()
                self.cookies.set(value)
                self.status.set("Cookies retrieved successfully!")
                messagebox.showinfo("Success", "Browser cookies were captured successfully! You can now start the scrape.", parent=self.root)
            else:
                self.running = False
                self.start_button.state(["!disabled"])
                self.progress.stop()
                if event == "done":
                    self.status.set(f"Saved: {value}")
                    messagebox.showinfo("Scrape complete", f"Results saved to:\n{value}", parent=self.root)
                else:
                    self.status.set("The operation did not complete.")
                    messagebox.showerror("Failed", value, parent=self.root)
                    
        self.root.after(100, self._process_events)


def main() -> None:
    root = tk.Tk()
    ScraperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()