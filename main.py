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


def describe_login_failure(error: str | None) -> str:
    if not error:
        return "No diagnostic was recorded. Check X account details and try again."

    normalized = error.casefold()
    stage_match = re.search(r"login_step=([a-z0-9_]+)", normalized)
    stage = f" during {stage_match.group(1)}" if stage_match else ""

    if any(term in normalized for term in ("attention required! | cloudflare", "you have been blocked", "cloudflare ray id:")):
        ray_match = re.search(r"cloudflare ray id:\s*<strong[^>]*>\s*([a-f0-9]+)", normalized)
        ray_id = f" Cloudflare Ray ID: {ray_match.group(1)}." if ray_match else ""
        return (
            "X returned a Cloudflare block page (HTTP 403) before sign-in; this does not indicate an incorrect "
            "password. Stop retrying for now. Ask corporate IT whether network policy, proxying, or TLS inspection "
            "is involved. If it is not, contact X support with the Ray ID."
            f"{ray_id}"
        )

    status_match = re.search(r"\b(400|401|403|407|429|5\d\d)\b", error)
    if status_match:
        status_code = status_match.group(1)
        if status_code == "407":
            return "The network proxy requires authentication or denied the connection. Check with corporate IT."
        if status_code == "429":
            return "X rate-limited the sign-in. Wait before trying again."
        if status_code in {"401", "403"}:
            return "X rejected or challenged the sign-in. Verify the account details and check whether X requires another verification step."
        return f"X or a network gateway returned an HTTP {status_code} error during sign-in. Check with corporate IT if this persists."

    if any(term in normalized for term in ("proxy", "407", "connecterror", "connecttimeout", "readtimeout", "timed out", "network is unreachable", "connection refused", "ssl", "certificate", "dns")):
        return (
            f"A network or TLS connection failed{stage}. A corporate proxy, firewall, DNS policy, or TLS inspection "
            "may be involved. The app does not change network or proxy settings; ask IT whether X and the email "
            "provider are reachable under your organization's policy."
        )
    if any(term in normalized for term in ("imap", "email", "loginacid")):
        return (
            f"Email verification failed{stage}. Check that IMAP is enabled and the email app password is correct; "
            "corporate policy may restrict IMAP access."
        )
    if any(term in normalized for term in ("mfa", "two-factor", "two factor", "authenticator")):
        return "This account appears to require authenticator-based verification, which this app does not currently support."

    return f"X sign-in failed{stage}. The raw server response is hidden to avoid exposing account or corporate network details."


async def ensure_account(
    api: API,
    username: str,
    password: str,
    email: str,
    email_password: str,
    status: Callable[[str], None],
) -> None:
    accounts = await api.pool.accounts_info()
    existing = next((account for account in accounts if account["username"].casefold() == username.casefold()), None)
    if existing and existing["active"]:
        status(f"Using saved session for @{username}.")
        return

    if not all((password, email, email_password)):
        raise ValueError(
            "First-time sign-in needs your X password, email address, and email app password. "
            "The email app password lets twscrape retrieve verification codes over IMAP."
        )

    status("Signing in to X. This can take a minute...")
    if existing:
        await api.pool.delete_accounts(username)
    await api.pool.add_account(username, password, email, email_password)
    login_errors = []
    sink_id = logger.add(
        login_errors.append,
        format="{message}",
        filter=lambda record: record["message"].startswith("Failed to login"),
        level="ERROR",
    )
    try:
        result = await api.pool.login_all([username])
    finally:
        logger.remove(sink_id)
    if result["success"] == 0:
        try:
            account = await api.pool.get(username)
            error = account.error_msg or (login_errors[-1] if login_errors else None)
        except Exception:
            error = login_errors[-1] if login_errors else None
        raise RuntimeError(describe_login_failure(error))
    status("Sign-in complete. Your session is saved on this computer.")


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
        self.password = tk.StringVar()
        self.email = tk.StringVar()
        self.email_password = tk.StringVar()
        self.tweet = tk.StringVar()

        root.title("Tweet Thread Scraper")
        root.geometry("620x620")
        root.minsize(560, 560)
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

        account = ttk.LabelFrame(shell, text="X account", style="Card.TLabelframe", labelanchor="nw")
        account.pack(fill="x", pady=(0, 14))
        account.columnconfigure(1, weight=1)
        self._field(account, "Username", self.username, 0)
        self._field(account, "X password", self.password, 1, show="*")
        self._field(account, "Email address", self.email, 2)
        self._field(account, "Email app password", self.email_password, 3, show="*")
        ttk.Label(
            account,
            text="Needed only for first sign-in. IMAP access is used for email verification codes.",
            wraplength=500,
            justify="left",
            background="#ffffff",
            foreground="#53616f",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

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
        ttk.Label(
            shell,
            text="twscrape stores account credentials and session data locally on this computer.",
            style="Subtitle.TLabel",
            wraplength=550,
        ).pack(anchor="w", side="bottom", pady=(16, 0))

    @staticmethod
    def _field(parent, label: str, variable: tk.StringVar, row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(parent, textvariable=variable, show=show or "").grid(row=row, column=1, sticky="ew", pady=4)

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_directory.get() or str(Path.home()))
        if selected:
            self.output_directory.set(selected)

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
            messagebox.showerror("Username required", "Enter the X username for the account to use.", parent=self.root)
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
        credentials = (username, self.password.get(), self.email.get().strip(), self.email_password.get())
        threading.Thread(
            target=self._run_job,
            args=(tweet_id, credentials, output_directory),
            daemon=True,
        ).start()

    def _run_job(self, tweet_id: str, credentials: tuple[str, str, str, str], output_directory: Path) -> None:
        username, password, email, email_password = credentials
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

                await ensure_account(api, username, password, email, email_password, report_status)
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
            else:
                self.running = False
                self.start_button.state(["!disabled"])
                self.progress.stop()
                if event == "done":
                    self.status.set(f"Saved: {value}")
                    messagebox.showinfo("Scrape complete", f"Results saved to:\n{value}", parent=self.root)
                else:
                    self.status.set("The scrape did not complete.")
                    messagebox.showerror("Scrape failed", value, parent=self.root)
        self.root.after(100, self._process_events)


def main() -> None:
    root = tk.Tk()
    ScraperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()