# Run this ONCE on your own computer (not on Railway):  python gen_session.py
# pip install telethon
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nLogged in as:", client.get_me().first_name)
    print("\nTG_SESSION =", client.session.save())
    print("\nKeep this string secret: anyone who has it controls your account.")
