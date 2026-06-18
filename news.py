"""
News Feed - tkinter + Google News RSS

A small desktop app that pulls recent headlines for a list of keywords
or topics from Google News RSS, filtered to a chosen set of publishers
and an optional date range, and shows them in a sortable table.
Select a row and press Enter (or double-click it) to open the article
in your browser.

Requirements (install once):
    pip install polars feedparser

Run:
    python news_feed_app.py
"""

import datetime
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from tkinter import font, messagebox, ttk

import feedparser
import polars as pl

# Publisher domains used to filter Google News results. Google News RSS
# doesn't reliably support "site:", so we AND the domain into the query
# instead (the same trick used in the original reference code).
SOURCES = [
    '"reuters.com"',
    '"bloomberg.com"',
    '"cnbc.com"',
    '"wsj.com"',
    '"marketwatch.com"',
    '"barrons.com"',
    '"businesswire.com"',
    '"finance.yahoo.com"',
    '"guardian"',
]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def safe_concat(frames):
    """Concatenate a list of DataFrames, ignoring empty ones."""
    frames = [f for f in frames if f is not None and f.height > 0]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def add_row_index(df, name="orig_index"):
    """polars renamed with_row_count -> with_row_index in newer versions."""
    if hasattr(df, "with_row_index"):
        return df.with_row_index(name)
    return df.with_row_count(name)


def fetch_google_news(keywords, start_date=None, end_date=None, selected_sources=None, log=None):
    """Fetch headlines for each keyword/topic from each selected source's RSS feed."""
    selected_sources = selected_sources or SOURCES
    frames = []

    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            continue
        for src in selected_sources:
            if log:
                log(f"Fetching '{keyword}' from {src.strip(chr(34))}...")

            date_filter = ""
            if start_date:
                query_start = start_date - datetime.timedelta(days=1)
                date_filter += f" after:{query_start.strftime('%Y-%m-%d')}"
            if end_date:
                query_end = end_date + datetime.timedelta(days=1)
                date_filter += f" before:{query_end.strftime('%Y-%m-%d')}"
            query = f'"{keyword}" AND {src}{date_filter}'
            encoded_query = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en&gl=US&ceid=US:en"

            feed = feedparser.parse(url)
            rows = []
            for entry in feed.entries:
                published = entry.get("published_parsed")
                if not published:
                    continue
                pub_date = datetime.date(*published[:3])
                if start_date and pub_date < start_date:
                    continue
                if end_date and pub_date > end_date:
                    continue
                rows.append({
                    "date": pub_date,
                    "topic": keyword,
                    "source": src.strip('"'),
                    "headline": entry.get("title", ""),
                    "link": entry.get("link"),
                })
            if rows:
                frames.append(pl.DataFrame(rows))

            time.sleep(0.5)  # be polite to Google News

    result = safe_concat(frames)
    if result.height:
        result = result.unique(subset="headline").sort("date", descending=True)
    return result


def handle_link(df, index):
    """Open the link for a given row index, if it's a safe http(s) URL."""
    link = df["link"][index]
    if link is None:
        return
    if link.startswith("https://") or link.startswith("http://"):
        webbrowser.open_new(link)
    else:
        print(f"Blocked unsafe link: {link}")


def send_notification(root, headline):
    """Show a desktop notification. Falls back to a tkinter popup toast."""
    notified = False

    # Try plyer first (cross-platform)
    try:
        from plyer import notification
        notification.notify(
            title="News Alert",
            message=headline[:200],
            app_name="News Feed",
            timeout=8,
        )
        notified = True
    except Exception:
        pass

    # macOS osascript fallback
    if not notified:
        try:
            import subprocess
            safe = headline.replace('"', "'")[:200]
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{safe}" with title "News Alert"'
            ])
            notified = True
        except Exception:
            pass

    # Final fallback: small toast window in tkinter
    if not notified:
        def _toast():
            toast = tk.Toplevel(root)
            toast.title("News Alert")
            toast.geometry("420x80+40+40")
            toast.attributes("-topmost", True)
            tk.Label(toast, text=headline[:120], wraplength=400, justify="left", padx=10, pady=10).pack()
            toast.after(7000, toast.destroy)
        root.after(0, _toast)


# ---------------------------------------------------------------------------
# Table widgets
# ---------------------------------------------------------------------------

def build_table(parent, df, fontsize=16):
    style = ttk.Style()
    style.configure("Treeview", rowheight=fontsize + 8)

    tree = ttk.Treeview(parent, columns=list(df.columns), show="headings")
    vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    parent.grid_rowconfigure(0, weight=1)
    parent.grid_columnconfigure(0, weight=1)

    default_font = font.Font(family="Helvetica", size=fontsize)
    tree.tag_configure("default_row", font=default_font)
    return tree, vsb, hsb, default_font


def add_display_table(parent, df, title="Data Viewer", to_pct=None, on_select=None):
    frame = ttk.LabelFrame(parent, text=title)
    frame.pack(expand=True, fill="both", padx=8, pady=8)

    table_frame = ttk.Frame(frame)
    table_frame.pack(expand=True, fill="both")

    tree, vsb, hsb, default_font = build_table(table_frame, df, 13)

    if to_pct is not None:
        to_pct = [x.lower() for x in to_pct]
    to_pct = set(to_pct or [])

    indexed_df = add_row_index(df, "orig_index")
    original_df = indexed_df.clone()
    sort_states = {col: 2 for col in df.columns}
    item_to_orig_index = {}
    display_cols = list(df.columns)

    def format_value(col, val):
        if val is None:
            return ""
        if col.lower() in to_pct:
            try:
                return f"{float(val) * 100:.2f}%"
            except (TypeError, ValueError):
                return str(val)
        return str(val)

    def populate(data):
        tree.delete(*tree.get_children())
        item_to_orig_index.clear()
        for row in data.iter_rows(named=True):
            values = [format_value(c, row[c]) for c in display_cols]
            item_id = tree.insert("", "end", values=values, tags=("default_row",))
            item_to_orig_index[item_id] = row["orig_index"]

    def sort_by(col):
        state = sort_states.get(col, 2)
        for c in sort_states:
            if c != col:
                sort_states[c] = 2
        if state == 2:
            sorted_df = indexed_df.sort(col, descending=False)
            sort_states[col] = 0
        elif state == 0:
            sorted_df = indexed_df.sort(col, descending=True)
            sort_states[col] = 1
        else:
            sorted_df = original_df
            sort_states[col] = 2
        populate(sorted_df)

    def measure_col_width(col):
        longest = default_font.measure(str(col))
        for val in df[col].to_list():
            text_width = default_font.measure(format_value(col, val))
            if text_width > longest:
                longest = text_width
        return max(50, min(longest + 24, 220))

    narrow_cols = display_cols[:-1]
    wide_col = display_cols[-1] if display_cols else None

    for col in narrow_cols:
        tree.heading(col, text=col, command=lambda c=col: sort_by(c))
        tree.column(col, width=measure_col_width(col), anchor="w", stretch=False)

    if wide_col is not None:
        tree.heading(wide_col, text=wide_col, command=lambda c=wide_col: sort_by(c))
        tree.column(wide_col, width=320, anchor="w", stretch=True)

    populate(indexed_df)

    def open_selected(event=None):
        selection = tree.selection()
        if not selection:
            return
        orig_idx = item_to_orig_index.get(selection[0])
        if orig_idx is not None and on_select is not None:
            on_select(orig_idx)

    if on_select is not None:
        tree.bind("<Double-1>", open_selected)
        tree.bind("<Return>", open_selected)
        tree.bind("<KP_Enter>", open_selected)

    return tree


def create_News(parent, df):
    container = ttk.Frame(parent)
    container.pack(expand=True, fill="both")
    if df.height == 0:
        ttk.Label(container, text="No headlines found.", padding=20).pack()
        return container
    add_display_table(
        container,
        df=df.select(["date", "topic", "source", "headline"]),
        title="News",
        on_select=lambda idx: handle_link(df, idx),
    )
    return container


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class NewsFeedApp:
    def __init__(self, root):
        self.root = root
        self.root.title("News Feed - Google News RSS")
        self.root.geometry("1000x700")

        self.results_df = pl.DataFrame()
        self.source_vars = {}

        # Alert state
        self._alert_active = False
        self._alert_thread = None
        self._seen_headlines = set()

        self._build_controls()
        self._build_results_area()

    def _build_controls(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Keywords (comma-separated):").grid(row=0, column=0, sticky="w")
        self.keywords_entry = ttk.Entry(top, width=40)
        self.keywords_entry.insert(0, "artificial intelligence, climate change")
        self.keywords_entry.grid(row=0, column=1, sticky="w", padx=(5, 20))
        self.keywords_entry.bind("<Return>", self.on_fetch_click)

        ttk.Label(top, text="Start date (YYYY-MM-DD, optional):").grid(row=0, column=2, sticky="w")
        self.start_date_entry = ttk.Entry(top, width=12)
        self.start_date_entry.grid(row=0, column=3, sticky="w", padx=(5, 10))
        self.start_date_entry.bind("<Return>", self.on_fetch_click)

        ttk.Label(top, text="End date (YYYY-MM-DD, optional):").grid(row=0, column=4, sticky="w")
        self.end_date_entry = ttk.Entry(top, width=12)
        self.end_date_entry.grid(row=0, column=5, sticky="w", padx=(5, 20))
        self.end_date_entry.bind("<Return>", self.on_fetch_click)

        self.fetch_button = ttk.Button(top, text="Fetch News", command=self.on_fetch_click)
        self.fetch_button.grid(row=0, column=6, sticky="w")

        sources_frame = ttk.LabelFrame(self.root, text="Sources", padding=10)
        sources_frame.pack(fill="x", padx=10, pady=(0, 5))

        for i, src in enumerate(SOURCES):
            label = src.strip('"')
            var = tk.BooleanVar(value=False)
            self.source_vars[src] = var
            ttk.Checkbutton(sources_frame, text=label, variable=var).grid(
                row=i // 4, column=i % 4, sticky="w", padx=10, pady=2
            )

        alerts_frame = ttk.LabelFrame(self.root, text="Alerts", padding=10)
        alerts_frame.pack(fill="x", padx=10, pady=(0, 5))

        ttk.Label(alerts_frame, text="Frequency (minutes):").grid(row=0, column=0, sticky="w")
        self.freq_entry = ttk.Entry(alerts_frame, width=12)
        self.freq_entry.insert(0, "5")
        self.freq_entry.grid(row=0, column=1, sticky="w", padx=(5, 20))

        self.alert_button = ttk.Button(
            alerts_frame, text="Alerts: Off", command=self.toggle_alerts
        )
        self.alert_button.grid(row=0, column=2, sticky="w")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, padding=(10, 0)).pack(fill="x")

    def _build_results_area(self):
        self.results_container = ttk.Frame(self.root)
        self.results_container.pack(expand=True, fill="both", padx=10, pady=10)
        ttk.Label(self.results_container, text="", padding=20).pack()

    # ------------------------------------------------------------------
    # Manual fetch
    # ------------------------------------------------------------------

    def on_fetch_click(self, event=None):
        raw_keywords = self.keywords_entry.get()
        keywords = [s for s in (x.strip() for x in raw_keywords.split(",")) if s]
        if not keywords:
            messagebox.showwarning("No keywords", "Enter at least one keyword or topic.")
            return

        selected_sources = [src for src, var in self.source_vars.items() if var.get()]
        if not selected_sources:
            messagebox.showwarning("No sources", "Select at least one news source.")
            return

        start_date = self._parse_date(self.start_date_entry.get())
        if start_date is False:
            return
        end_date = self._parse_date(self.end_date_entry.get())
        if end_date is False:
            return
        if start_date and end_date and start_date > end_date:
            messagebox.showerror("Invalid date range", "Start date must be on or before the end date.")
            return

        self.fetch_button.config(state="disabled")
        self.status_var.set("Fetching news...")
        thread = threading.Thread(
            target=self._fetch_worker,
            args=(keywords, start_date, end_date, selected_sources, False),
            daemon=True,
        )
        thread.start()

    def _parse_date(self, raw_date):
        raw_date = raw_date.strip()
        if not raw_date:
            return None
        try:
            return datetime.datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("Invalid date", "Use the YYYY-MM-DD format for dates.")
            return False

    def _fetch_worker(self, keywords, start_date, end_date, selected_sources, alert_mode):
        def log(msg):
            self.root.after(0, lambda: self.status_var.set(msg))

        try:
            df = fetch_google_news(keywords, start_date, end_date, selected_sources, log=log)
        except Exception as exc:
            self.root.after(0, lambda: self._on_fetch_error(exc))
            return
        self.root.after(0, lambda: self._on_fetch_done(df, alert_mode))

    def _on_fetch_done(self, df, alert_mode=False):
        if alert_mode:
            # Find headlines we haven't seen before and notify
            new_rows = [
                h for h in (df["headline"].to_list() if df.height else [])
                if h not in self._seen_headlines
            ]
            for headline in new_rows:
                self._seen_headlines.add(headline)
                send_notification(self.root, headline)
                self.status_var.set(f"Alert: {headline[:80]}…")

            # Merge new results into main df and refresh table
            if new_rows:
                combined = safe_concat([self.results_df, df])
                if combined.height:
                    combined = combined.unique(subset="headline").sort("date", descending=True)
                self.results_df = combined
                for child in self.results_container.winfo_children():
                    child.destroy()
                create_News(self.results_container, self.results_df)
        else:
            self.results_df = df
            # Seed seen headlines so first alert run only notifies truly new ones
            if df.height:
                self._seen_headlines.update(df["headline"].to_list())
            for child in self.results_container.winfo_children():
                child.destroy()
            create_News(self.results_container, df)
            self.fetch_button.config(state="normal")
            self.status_var.set(f"Done. {df.height} headline(s) found.")

    def _on_fetch_error(self, exc):
        self.fetch_button.config(state="normal")
        self.status_var.set("Error while fetching news.")
        messagebox.showerror("Fetch failed", str(exc))

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def toggle_alerts(self):
        if not self._alert_active:
            self._start_alerts()
        else:
            self._stop_alerts()

    def _start_alerts(self):
        raw = self.freq_entry.get().strip()
        try:
            minutes = float(raw)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid frequency", "Enter a positive number of minutes.")
            return

        raw_keywords = self.keywords_entry.get()
        keywords = [s for s in (x.strip() for x in raw_keywords.split(",")) if s]
        if not keywords:
            messagebox.showwarning("No keywords", "Enter at least one keyword or topic.")
            return

        selected_sources = [src for src, var in self.source_vars.items() if var.get()]
        if not selected_sources:
            messagebox.showwarning("No sources", "Select at least one news source.")
            return

        self._alert_active = True
        self.alert_button.config(text="Alerts: On")
        self.freq_entry.config(state="disabled")
        self.status_var.set("Alerts on — fetching now…")

        self._alert_thread = threading.Thread(
            target=self._alert_loop,
            args=(keywords, selected_sources, minutes),
            daemon=True,
        )
        self._alert_thread.start()

    def _stop_alerts(self):
        self._alert_active = False
        self.alert_button.config(text="Alerts: Off")
        self.freq_entry.config(state="normal")
        self.status_var.set("Alerts off.")

    def _alert_loop(self, keywords, selected_sources, minutes):
        interval_seconds = minutes * 60

        def log(msg):
            self.root.after(0, lambda: self.status_var.set(msg))

        first_run = True
        while self._alert_active:
            log("Alerts: fetching...")
            try:
                df = fetch_google_news(keywords, None, None, selected_sources, log=log)
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.status_var.set(f"Alert fetch error: {e}"))
                df = pl.DataFrame()

            if self._alert_active:
                if first_run:
                    # Seed seen headlines silently - no notifications on first run
                    def _seed(d=df):
                        if d.height:
                            self._seen_headlines.update(d["headline"].to_list())
                        self._on_fetch_done(d, alert_mode=False)
                    self.root.after(0, _seed)
                    first_run = False
                else:
                    self.root.after(0, lambda d=df: self._on_fetch_done(d, alert_mode=True))

            elapsed = 0
            while self._alert_active and elapsed < interval_seconds:
                time.sleep(1)
                elapsed += 1

            if self._alert_active:
                log(f"Alerts: next fetch in {minutes:.0f} min...")


if __name__ == "__main__":
    root = tk.Tk()
    app = NewsFeedApp(root)
    root.mainloop()