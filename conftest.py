import os
os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ESCALATION_CHAT_ID", "123456")
os.environ.setdefault("MANAGER_TG_ID", "0")
