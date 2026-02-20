"""
Reddit data adapter — PRAW.
Provides: ticker mentions, sentiment data from subreddits.
Stubbed for initial build — real PRAW integration ready.
"""

import re
from utils.logger import get_logger

log = get_logger("reddit_data")

SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "options",
]


class RedditDataAdapter:
    def __init__(self, client_id: str = "", client_secret: str = "", user_agent: str = "SwingTrader/1.0"):
        self.reddit = None
        if client_id and client_secret:
            try:
                import praw
                self.reddit = praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_agent=user_agent,
                )
                log.info("reddit_connected")
            except Exception as e:
                log.warning("reddit_init_failed", error=str(e))

    def get_ticker_mentions(self, ticker: str, days: int = 1, min_upvotes: int = 10) -> list[dict]:
        """Get posts mentioning a ticker from monitored subreddits."""
        if not self.reddit:
            return []
        mentions = []
        pattern = re.compile(rf'\b{re.escape(ticker)}\b', re.IGNORECASE)
        for sub_name in SUBREDDITS:
            try:
                subreddit = self.reddit.subreddit(sub_name)
                for post in subreddit.new(limit=100):
                    if post.score < min_upvotes:
                        continue
                    text = f"{post.title} {post.selftext}"
                    if pattern.search(text):
                        mentions.append({
                            "subreddit": sub_name,
                            "title": post.title,
                            "score": post.score,
                            "num_comments": post.num_comments,
                            "created_utc": post.created_utc,
                            "url": post.url,
                            "text_preview": text[:500],
                        })
            except Exception as e:
                log.error("reddit_fetch_failed", subreddit=sub_name, error=str(e))
                continue
        return mentions

    def get_trending_tickers(self, universe: list[str]) -> dict[str, int]:
        """Count mentions of each ticker across subreddits."""
        if not self.reddit:
            return {}
        counts = {}
        for sub_name in SUBREDDITS[:2]:  # Limit to top 2 for speed
            try:
                subreddit = self.reddit.subreddit(sub_name)
                for post in subreddit.hot(limit=50):
                    text = f"{post.title} {post.selftext}".upper()
                    for ticker in universe:
                        if f" {ticker} " in f" {text} " or f"${ticker}" in text:
                            counts[ticker] = counts.get(ticker, 0) + 1
            except Exception:
                continue
        return counts
