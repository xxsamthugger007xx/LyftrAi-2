import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


class Settings:
    def __init__(self) -> None:
        self.api_keys = [k.strip() for k in os.getenv("LLM_API_KEYS", "").split(",") if k.strip()]
        self.database_url = os.getenv("DATABASE_URL", "sqlite:////data/app.db")
        self.allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
        self.simulation_enabled = _bool("ENABLE_FAILURE_SIMULATION")
        self.model = os.getenv("LLM_MODEL", "gemini/gemini-3.1-flash-lite")
        self.default_system_prompt = os.getenv("DEFAULT_SYSTEM_PROMPT", "You are a helpful assistant.")
        self.request_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "12"))
        self.cache_enabled = _bool("CACHE_ENABLED", True)

    @property
    def db_path(self) -> str:
        url = self.database_url
        if url.startswith("sqlite:///"):
            return url[len("sqlite:///"):]  # sqlite:////data/app.db -> /data/app.db
        return url
