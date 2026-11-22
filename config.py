import os
from dotenv import load_dotenv

load_dotenv()

def _clean(name: str, default: str = "") -> str:
    """Env value without stray spaces or quotes (Render/Railway dashboards keep
    quotes literally, so OWNER_ID="123" would otherwise break int())."""
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().strip('"').strip("'").strip()
    return val if val else default


def _int_env(name: str):
    """Numeric ID from env (e.g. -1001234567890). None if not set / not a number."""
    val = _clean(name)
    return int(val) if val.lstrip("-").isdigit() else None


BOT_TOKEN = _clean("BOT_TOKEN")
OWNER_ID = _int_env("OWNER_ID") or 0
# Second admin (optional) — gets the same admin commands as the owner. 0 = none.
CO_OWNER_ID = _int_env("CO_OWNER_ID") or 0
API_ID = _int_env("API_ID") or 0
API_HASH = _clean("API_HASH") or None
MONGO_URI = _clean("MONGO_URI")
# Separate database name inside the same Mongo cluster, so this bot never
# touches another bot's data. Change only if you want a different name.
DATABASE_NAME = _clean("DATABASE_NAME", "zerox_otp_bot")
UPI_ID = _clean("UPI_ID", "Not Set")
CRYPTO_ADDRESS = _clean("CRYPTO_ADDRESS", "Not Set")  # crypto deposit is still "unavailable" in the bot


# Force-join channels shown in /start. Only these — the private admin log
# group is NOT in this list.
#   chat : what Telegram checks membership against (@username, or numeric ID
#          for a private channel). None = membership is not checked for it.
#   link : the button users tap to join.
REQUIRED_CHANNELS = [
    # Private channel (invite link) — needs its numeric ID in FORCE_JOIN_PRIVATE_ID
    {"chat": _int_env("FORCE_JOIN_PRIVATE_ID"), "link": "https://t.me/+Gw6-s632_fI5NTQ1"},
    {"chat": "@Logs_4888", "link": "https://t.me/Logs_4888"},
    {"chat": "@ZEROX_8999", "link": "https://t.me/ZEROX_8999"},
]

# Private admin group that receives the detailed sale / OTP logs
# (invite link: https://t.me/+ENqrs28FfjFmYTE1). Bot must be a member/admin there.
# Needs the group's numeric ID. Not set = detailed logs are skipped.
ADMIN_LOG_GROUP_ID = _int_env("ADMIN_LOG_GROUP_ID")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing!")
if not MONGO_URI:
    raise ValueError("MONGO_URI is missing!")

# Optional: Fernet key that protects the daily backup file (sessions included, encrypted).
# Without it the backup is still sent, but session strings / 2FA / API keys are left out.
# Make one with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
BACKUP_KEY = _clean("BACKUP_KEY")

