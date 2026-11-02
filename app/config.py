"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed policy for model calls, fetching, and presentation."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- OpenAI -------------------------------------------------------------
    # Deliberately has no default and no fallback planner: a missing key is a
    # loud misconfiguration, not a reason to silently degrade.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_timeout_s: float = 20.0

    # --- ClinicalTrials.gov fetch policy ------------------------------------
    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    page_size: int = 1000
    # Sized so that a single drug or specific condition (typically 2-4k trials)
    # is fetched completely rather than sampled: at ~0.85 s per 1,000 records a
    # census costs ~3 s, and because the API's default order is relevance rather
    # than random, a census removes sampling bias outright instead of merely
    # narrowing it. Broad queries still need a ceiling -- "cancer" is ~124k
    # trials, ~106 s -- so those sample, with the shortfall disclosed.
    max_records: int = 5000
    max_records_hard_cap: int = 10000
    fetch_budget_s: float = 25.0
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 15.0
    max_retries: int = 3
    user_agent: str = "clinical-trials-viz/0.1 (+https://github.com/see-clinical-trials)"

    # --- Stratified (exact-count) fetch -------------------------------------
    exact_counts: bool = True
    exact_max_buckets: int = 32
    exact_concurrency: int = 6
    exact_page_size: int = 200

    # --- Aggregation / presentation -----------------------------------------
    top_n: int = 10
    citation_limit: int = 5
    cache_ttl_s: float = 900.0
    cache_max_entries: int = 64

    @property
    def record_cap(self) -> int:
        """Return the configured default without exceeding the safety ceiling."""
        return min(self.max_records, self.max_records_hard_cap)


settings = Settings()
