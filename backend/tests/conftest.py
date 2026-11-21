import os
import tempfile

# app.main builds a module-level `app` at import time; point it at a throwaway DB before it is imported.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "import.db"))
os.environ.setdefault("LLM_API_KEYS", "import-key-1,import-key-2")
