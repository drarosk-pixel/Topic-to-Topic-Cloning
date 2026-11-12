"""
Telegram Topic-Group Cloner Bot
===============================

A BotFather bot that drives a per-user *userbot* created at runtime through
an in-chat /login flow (phone -> OTP -> optional 2FA). No session string ever
needs to be placed in the environment.

Once logged in, /clone asks for a source forum group id (-100...) and makes a
pixel-perfect copy: same title, same topics (name + icon), and every topic's
messages copied in order via server-side forwarding (no download/upload).

Commands
--------
  /start, /help  - show help
  /login         - log a userbot in with your phone number
  /logout        - drop the stored session
  /clone         - clone a forum group (asks for the -100 id)
  /cancel        - cancel the current flow
"""

import asyncio
import re

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
)

import config
import session_store
from cloner import clone_forum


bot = TelegramClient("bot_session", config.API_ID, config.API_HASH)

# Runtime state (in-memory)
user_states = {}    # uid -> {"step": ..., ...}
user_clients = {}   # uid -> authorized userbot TelegramClient
login_tmp = {}      # uid -> temp client during the login handshake


HELP_TEXT = (
    "**Topic Group Cloner**\n\n"
    "1. /login  - apne userbot ko phone + OTP se login karo "
    "(session string ki zarurat nahi).\n"
    "2. /clone  - source forum group ki `-100...` id bhejo, bot hubohu "
    "copy bana dega (title, topics, aur har topic ka content).\n\n"
    "Baaki: /logout  /cancel  /help\n\n"
    "Note: Source group me forwarding on honi chahiye (content protected "
    "nahi), tabhi server-side copy bina download/upload ke hoga."
)


def _progress_bar(done, total, width=14):
    """Render a text progress bar like '▰▰▰▱▱▱▱▱▱▱▱▱▱▱ 21%'."""
    if not total or total <= 0:
        total = 1
    done = max(0, min(done, total))
    filled = int(round(width * done / total))
    pct = int(round(100 * done / total))
    return "▰" * filled + "▱" * (width - filled) + f"  {pct}%"


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/(start|help)\b"))
async def cmd_help(event):
    await event.respond(HELP_TEXT)


@bot.on(events.NewMessage(pattern=r"^/cancel\b"))
async def cmd_cancel(event):
    uid = event.sender_id
    user_states.pop(uid, None)
    tmp = login_tmp.pop(uid, None)
    if tmp is not None:
        try:
            await tmp.disconnect()
        except Exception:
            pass
    await event.respond("Thik hai, cancel kar diya.")


@bot.on(events.NewMessage(pattern=r"^/login\b"))
async def cmd_login(event):
    uid = event.sender_id
    if uid in user_clients:
        await event.respond("Aap already logged in ho. Seedha /clone bhejo, "
                            "ya pehle /logout karo.")
        return
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    login_tmp[uid] = client
    user_states[uid] = {"step": "await_phone"}
    await event.respond(
        "Apna phone number country code ke saath bhejo.\n"
        "Example: `+919812345678`"
    )


@bot.on(events.NewMessage(pattern=r"^/logout\b"))
async def cmd_logout(event):
    uid = event.sender_id
    client = user_clients.pop(uid, None)
    if client is not None:
        try:
            await client.log_out()
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
    session_store.remove(config.SESSIONS_FILE, uid)
    user_states.pop(uid, None)
    await event.respond("Logout ho gaya. Session bhi hata diya.")


@bot.on(events.NewMessage(pattern=r"^/clone\b"))
async def cmd_clone(event):
    uid = event.sender_id
    if uid not in user_clients:
        await event.respond("Pehle /login karo.")
        return
    user_states[uid] = {"step": "await_source"}
    await event.respond(
        "Source forum group ki id bhejo (jaise `-1001234567890`).\n"
        "Dhyan: aapka userbot us group ka member hona chahiye."
    )


# --------------------------------------------------------------------------- #
# Free-text state machine (phone / otp / password / source id)
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage)
async def on_message(event):
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return  # commands handled above
    uid = event.sender_id
    st = user_states.get(uid)
    if not st:
        return

    step = st["step"]
    if step == "await_phone":
        await _handle_phone(event, uid, st, text)
    elif step == "await_code":
        await _handle_code(event, uid, st, text)
    elif step == "await_password":
        await _handle_password(event, uid, st, text)
    elif step == "await_source":
        await _handle_source(event, uid, text)


async def _handle_phone(event, uid, st, text):
    client = login_tmp.get(uid)
    if client is None:
        user_states.pop(uid, None)
        await event.respond("Session expire ho gaya. Dubara /login karo.")
        return
    phone = text.replace(" ", "")
    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await event.respond("Ye number invalid laga. Country code ke saath "
                            "dubara bhejo (jaise +91...).")
        return
    st["phone"] = phone
    st["hash"] = sent.phone_code_hash
    st["step"] = "await_code"
    await event.respond(
        "Telegram par jo OTP aaya hai use **spaces ke saath** bhejo, "
        "jaise `1 2 3 4 5`.\n\n"
        "(Spaces zaroori hain — plain code bhejne par Telegram use invalid "
        "kar deta hai security ke liye.)"
    )


async def _handle_code(event, uid, st, text):
    client = login_tmp.get(uid)
    if client is None:
        user_states.pop(uid, None)
        await event.respond("Session expire ho gaya. Dubara /login karo.")
        return
    code = re.sub(r"\D", "", text)
    if not code:
        await event.respond("Code me digits nahi mile. Dubara bhejo "
                            "(jaise `1 2 3 4 5`).")
        return
    try:
        await client.sign_in(st["phone"], code, phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        st["step"] = "await_password"
        await event.respond("Account par 2FA hai. Apna password bhejo.")
        return
    except PhoneCodeInvalidError:
        await event.respond("Galat code. Dubara try karo (spaces ke saath).")
        return
    except PhoneCodeExpiredError:
        user_states.pop(uid, None)
        await event.respond("Code expire ho gaya. Dubara /login karo.")
        return
    await _finish_login(event, uid)


async def _handle_password(event, uid, st, text):
    client = login_tmp.get(uid)
    if client is None:
        user_states.pop(uid, None)
        await event.respond("Session expire ho gaya. Dubara /login karo.")
        return
    try:
        await client.sign_in(password=text)
    except PasswordHashInvalidError:
        await event.respond("Password galat hai. Dubara bhejo.")
        return
    await _finish_login(event, uid)


async def _finish_login(event, uid):
    client = login_tmp.pop(uid)
    session_string = client.session.save()
    session_store.save(config.SESSIONS_FILE, uid, session_string)
    user_clients[uid] = client
    user_states.pop(uid, None)
    me = await client.get_me()
    name = me.first_name or "user"
    await event.respond(
        f"Login ho gaya as **{name}**. "
        f"Ab /clone bhejo aur source group ki `-100...` id do."
    )


async def _handle_source(event, uid, text):
    user_states.pop(uid, None)
    client = user_clients.get(uid)
    if client is None:
        await event.respond("Pehle /login karo.")
        return

    raw = text.strip().split()[0]
    source = raw
    if re.fullmatch(r"-?\d+", raw):
        source = int(raw)

    status = await event.respond("Clone shuru kar raha hoon...")

    async def progress(stage, info):
        try:
            if stage == "create":
                await status.edit(f"🆕 Naya group bana raha hoon: "
                                  f"**{info['title']}**")
            elif stage == "topic":
                await status.edit(f"🧵 Topic bana: {info['title']}")
            elif stage == "messages":
                bar = _progress_bar(info["index"], info["topics_total"])
                await status.edit(
                    f"📋 Cloning...\n\n"
                    f"{bar}\n"
                    f"Topics: {info['index']}/{info['topics_total']}\n"
                    f"Abhi hua: **{info['title']}** ({info['count']} msgs)\n"
                    f"Total messages: **{info['total']}**"
                )
        except Exception:
            pass

    try:
        dest, total, skipped = await clone_forum(
            client, source, progress,
            sleep_between=config.SLEEP_BETWEEN,
            sleep_per_topic=config.SLEEP_PER_TOPIC,
        )
    except Exception as e:
        await event.respond(f"Error aa gaya: `{e}`")
        return

    msg = (
        f"✅ Done!\nNaya group: **{dest.title}**\n"
        f"Total messages copy hue: **{total}**\n\n"
        f"Group aapke account me khul gaya hoga (aap owner ho)."
    )
    if skipped:
        details = "\n".join(f"• {s}" for s in skipped[:10])
        msg += f"\n\n⚠️ {len(skipped)} cheezein skip hui:\n{details}"
    await event.respond(msg)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
async def _restore_sessions():
    data = session_store.load_all(config.SESSIONS_FILE)
    for uid_str, s in data.items():
        try:
            client = TelegramClient(StringSession(s), config.API_ID,
                                    config.API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[int(uid_str)] = client
            else:
                await client.disconnect()
        except Exception:
            pass


async def main():
    import telethon
    print(f"Telethon version: {getattr(telethon, '__version__', '?')}")
    await _restore_sessions()
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    print(f"Bot chal raha hai as @{me.username}")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
