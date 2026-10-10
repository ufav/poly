import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

wallet = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
relayer_key = os.getenv("POLYMARKET_RELAYER_API_KEY", "")
relayer_address = os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "")

ok = True
print(f"POLYMARKET_WALLET_ADDRESS:          {wallet or 'EMPTY'}")

try:
    signer = Account.from_key(private_key).address
    print(f"signer (из POLYMARKET_PRIVATE_KEY): {signer}")
except Exception:
    signer = ""
    ok = False
    print("POLYMARKET_PRIVATE_KEY:             невалидный или пустой")

print(f"POLYMARKET_RELAYER_API_KEY_ADDRESS: {relayer_address or 'EMPTY'}")
if signer and relayer_address and signer.lower() != relayer_address.lower():
    ok = False
    print("  -> НЕ совпадает с signer: ключ не от того аккаунта")

if not relayer_key or relayer_key.startswith("<"):
    ok = False
    print("POLYMARKET_RELAYER_API_KEY:         EMPTY")
else:
    print(f"POLYMARKET_RELAYER_API_KEY:         задан (len={len(relayer_key)})")

sys.exit(0 if ok else 1)
