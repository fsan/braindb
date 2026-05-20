import os

from pydantic_settings import BaseSettings, SettingsConfigDict

# LLM provider profiles. Flip the whole stack by setting LLM_PROFILE in .env.
# Each profile is a LiteLLM model prefix + the env var holding its API key,
# plus an optional base_url for self-hosted OpenAI-compatible servers (vLLM,
# Ollama, llama.cpp). Adding a new provider is a dict entry, no code change.
_LLM_PROFILES: dict[str, dict[str, str]] = {
    "nim": {
        "model": "nvidia_nim/google/gemma-4-31b-it",
        "api_key_env": "NVIDIA_NIM_API_KEY",
    },
    "deepinfra": {
        "model": "deepinfra/google/gemma-4-31B-it",
        "api_key_env": "DEEPINFRA_API_KEY",
    },
    "vllm_workstation": {
        "model": "openai/cyankiwi/gemma-4-31B-it-AWQ-4bit",
        "api_key_env": "VLLM_API_KEY",
        "base_url": "http://host.docker.internal:8002/v1",
    },
    "vllm_workstation_qwen": {
        "model": "openai/cyankiwi/Qwen3.6-27B-AWQ-INT4",
        "api_key_env": "VLLM_API_KEY",
        "base_url": "http://host.docker.internal:8010/v1",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://braindb:braindb@localhost:5432/braindb"
    api_port: int = 8000

    # Temporal decay rates per entity type (per day)
    decay_rate_thought: float = 0.005
    decay_rate_fact: float = 0.001
    decay_rate_source: float = 0.002
    decay_rate_datasource: float = 0.001
    decay_rate_rule: float = 0.0
    decay_rate_wiki: float = 0.0   # synthesised pages should not fade

    # Graph traversal
    max_graph_depth: int = 3
    min_relevance_threshold: float = 0.05
    level_decay: list[float] = [1.0, 0.6, 0.3]

    # Scoring
    missing_signal_penalty: float = 0.5   # multiplier when only text OR only embedding matches (0-1)

    # Scoring-pool caps. These bound the CANDIDATE pool that feeds ranking
    # (pure SQL/vector work — cheap, runs once per query). They are NOT the
    # LLM-visible cap; the caller's `max_results` truncates the FINAL sorted
    # items list. Keeping these wide is essential: a narrow single-word
    # keyword (e.g. "Petros") embedded against a multi-word sentence query
    # may not place in the top 30 most-similar keywords even when it's the
    # exact match — without enough headroom, nothing tagged with that
    # keyword enters the scoring pool at all.
    scoring_pool_keyword_neighbors: int = 500   # top-K keyword embeddings to consider
    scoring_pool_fuzzy: int = 500               # top-K fuzzy/full-text candidates to consider

    # Always-on rules cap
    max_always_on_rules: int = 10

    # Agent (LiteLLM — provider selected via llm_profile)
    llm_profile: str = "deepinfra"
    agent_model: str = ""          # blank = use profile's default model
    agent_max_turns: int = 15
    agent_subagent_max_turns: int = 30
    agent_verbose: bool = False

    @property
    def resolved_agent_model(self) -> str:
        return self.agent_model or _LLM_PROFILES[self.llm_profile]["model"]

    @property
    def resolved_api_key(self) -> str:
        profile = _LLM_PROFILES[self.llm_profile]
        key = os.getenv(profile["api_key_env"], "")
        # Self-hosted profiles (vLLM/Ollama) may run without auth, but the
        # OpenAI client still needs a non-empty key — supply a placeholder.
        if not key and profile.get("base_url"):
            return "EMPTY"
        return key

    @property
    def resolved_base_url(self) -> str | None:
        return _LLM_PROFILES[self.llm_profile].get("base_url")


settings = Settings()
