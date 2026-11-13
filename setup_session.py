import os
import asyncio
from getpass import getpass
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id=os.getenv("TG_API_ID") or input("Telegram API ID: ").strip()
    api_hash=os.getenv("TG_API_HASH") or getpass("Telegram API HASH: ")
    phone=input("Second Telegram account phone (+countrycode...): ").strip()
    client=TelegramClient(StringSession(),int(api_id),api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
        code=input("Telegram login code: ").strip()
        try:
            await client.sign_in(phone,code)
        except Exception as exc:
            if exc.__class__.__name__ != "SessionPasswordNeededError": raise
            password=getpass("Two-step verification password: ")
            await client.sign_in(password=password)
        print("\nTG_SESSION_STRING (keep secret, add only in Railway):\n")
        print(client.session.save())
    finally:
        await client.disconnect()

if __name__=="__main__":
    asyncio.run(main())
