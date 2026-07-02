import os
os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ESCALATION_CHAT_ID", "123456")
os.environ.setdefault("MANAGER_TG_ID", "0")
# Keep the test DB seed to just the legacy Telegram account and never touch Docker.
os.environ.setdefault("WHATSAPP_AUTOSEED", "false")
os.environ.setdefault("WAHA_AUTOSTART", "false")
