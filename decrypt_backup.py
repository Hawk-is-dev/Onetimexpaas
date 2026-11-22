"""
Decrypts a backup made with BACKUP_KEY.

Usage:
    python decrypt_backup.py backup_20261122_2200.json.gz.enc YOUR_BACKUP_KEY

Writes backup_20261122_2200.json.gz next to it (a normal gzip file with the
whole database as JSON: gunzip it or open it with any JSON tool).
"""
import sys
from cryptography.fernet import Fernet

if len(sys.argv) != 3 or not sys.argv[1].endswith(".enc"):
    sys.exit(__doc__)

src, key = sys.argv[1], sys.argv[2]
out = src[:-4]
with open(src, "rb") as f:
    data = Fernet(key.encode()).decrypt(f.read())
with open(out, "wb") as f:
    f.write(data)
print(f"Decrypted -> {out}")
