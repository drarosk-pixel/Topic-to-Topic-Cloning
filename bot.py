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
import rules_store
import rules as rules_mod
from cloner import clone_forum


def _get_rules(uid):
    return rules_mod.normalize(rules_store.load(config.RULES_FILE, uid))


def _save_rules(uid, r):
    rules_store.save(config.RULES_FILE, uid, r)


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
    "**Content editing rules (optional):**\n"
    "Jab koi rule set ho, bot copy mode me jaata hai aur caption edit karta "
    "hai (media phir bhi bina download reuse hota hai).\n"
    "• /rules — current rules dekho\n"
    "• /caption <text> — har message me caption add karo (khali = hatao)\n"
    "• /captionpos top|bottom — caption upar ya neeche\n"
    "• /replaceword purana | naya — word replace\n"
    "• /removeword word — word hatao\n"
    "• /replacelink purana | naya — link/URL ka text replace\n"
    "• /removelinks on|off — saare hyperlink hatao (text rehne do)\n"
    "• /removelinkline on|off — jis line me link ho (chhupa hua bhi) poori "
    "line hatao\n"
    "• /removewordline word — jis line me ye word ho poori line hatao\n"
    "• /clearrules — saare rules reset\n\n"
    "Baaki: /logout  /cancel  /help\n\n"
    "Note: Source group me forwarding on honi chahiye (content protected "
    "nahi), tabhi copy bina download/upload ke hoga."
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
# Content-rule commands
# --------------------------------------------------------------------------- #
def _arg(raw_text):
    """Everything after the command word, stripped."""
    parts = raw_text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _onoff(val, default=False):
    v = val.strip().lower()
    if v in ("on", "yes", "true", "1"):
        return True
    if v in ("off", "no", "false", "0"):
        return False
    return default


@bot.on(events.NewMessage(pattern=r"^/rules\b"))
async def cmd_rules(event):
    r = _get_rules(event.sender_id)
    lines = ["**Aapke current rules:**"]
    lines.append(f"• Caption: {r['caption'] or '(none)'} "
                 f"[{r['caption_pos']}]")
    lines.append(f"• Remove all hyperlinks: "
                 f"{'ON' if r['remove_all_links'] else 'off'}")
    lines.append(f"• Remove lines with links: "
                 f"{'ON' if r['remove_link_lines'] else 'off'}")
    lines.append(f"• Word replace/remove: {r['word_replacements'] or '(none)'}")
    lines.append(f"• Link replace: {r['link_replacements'] or '(none)'}")
    lines.append(f"• Remove lines with words: "
                 f"{r['remove_word_lines'] or '(none)'}")
    mode = "COPY mode (edit)" if rules_mod.has_any(r) else "FORWARD mode (exact)"
    lines.append(f"\nClone chalega: **{mode}**")
    await event.respond("\n".join(lines))


@bot.on(events.NewMessage(pattern=r"^/clearrules\b"))
async def cmd_clearrules(event):
    rules_store.clear(config.RULES_FILE, event.sender_id)
    await event.respond("Saare rules reset ho gaye. Ab clone exact (forward) "
                        "mode me chalega.")


@bot.on(events.NewMessage(pattern=r"^/caption\b"))
async def cmd_caption(event):
    uid = event.sender_id
    r = _get_rules(uid)
    r["caption"] = _arg(event.raw_text)
    _save_rules(uid, r)
    if r["caption"]:
        await event.respond(f"Caption set ho gaya ({r['caption_pos']}):\n"
                            f"{r['caption']}")
    else:
        await event.respond("Caption hata diya.")


@bot.on(events.NewMessage(pattern=r"^/captionpos\b"))
async def cmd_captionpos(event):
    uid = event.sender_id
    pos = _arg(event.raw_text).lower()
    if pos not in ("top", "bottom"):
        await event.respond("Use: /captionpos top  ya  /captionpos bottom")
        return
    r = _get_rules(uid)
    r["caption_pos"] = pos
    _save_rules(uid, r)
    await event.respond(f"Caption position: {pos}")


@bot.on(events.NewMessage(pattern=r"^/replaceword\b"))
async def cmd_replaceword(event):
    uid = event.sender_id
    arg = _arg(event.raw_text)
    if "|" not in arg:
        await event.respond("Use: /replaceword purana | naya")
        return
    old, new = [p.strip() for p in arg.split("|", 1)]
    if not old:
        await event.respond("Purana word khali nahi ho sakta.")
        return
    r = _get_rules(uid)
    r["word_replacements"].append([old, new])
    _save_rules(uid, r)
    await event.respond(f"Rule add: '{old}' -> '{new}'")


@bot.on(events.NewMessage(pattern=r"^/removeword\b"))
async def cmd_removeword(event):
    uid = event.sender_id
    word = _arg(event.raw_text)
    if not word:
        await event.respond("Use: /removeword word")
        return
    r = _get_rules(uid)
    r["word_replacements"].append([word, ""])
    _save_rules(uid, r)
    await event.respond(f"Word hataya jayega: '{word}'")


@bot.on(events.NewMessage(pattern=r"^/replacelink\b"))
async def cmd_replacelink(event):
    uid = event.sender_id
    arg = _arg(event.raw_text)
    if "|" not in arg:
        await event.respond("Use: /replacelink purana | naya\n"
                            "(jaise: /replacelink t.me/old | t.me/new)")
        return
    old, new = [p.strip() for p in arg.split("|", 1)]
    if not old:
        await event.respond("Purana link khali nahi ho sakta.")
        return
    r = _get_rules(uid)
    r["link_replacements"].append([old, new])
    _save_rules(uid, r)
    await event.respond(f"Link rule add: '{old}' -> '{new}'")


@bot.on(events.NewMessage(pattern=r"^/removelinks\b"))
async def cmd_removelinks(event):
    uid = event.sender_id
    r = _get_rules(uid)
    r["remove_all_links"] = _onoff(_arg(event.raw_text), not r["remove_all_links"])
    _save_rules(uid, r)
    await event.respond(f"Remove all hyperlinks: "
                        f"{'ON' if r['remove_all_links'] else 'off'}")


@bot.on(events.NewMessage(pattern=r"^/removelinkline\b"))
async def cmd_removelinkline(event):
    uid = event.sender_id
    r = _get_rules(uid)
    r["remove_link_lines"] = _onoff(_arg(event.raw_text),
                                    not r["remove_link_lines"])
    _save_rules(uid, r)
    await event.respond(f"Remove lines containing links: "
                        f"{'ON' if r['remove_link_lines'] else 'off'}")


@bot.on(events.NewMessage(pattern=r"^/removewordline\b"))
async def cmd_removewordline(event):
    uid = event.sender_id
    word = _arg(event.raw_text)
    if not word:
        await event.respond("Use: /removewordline word")
        return
    r = _get_rules(uid)
    r["remove_word_lines"].append(word)
    _save_rules(uid, r)
    await event.respond(f"Jis line me '{word}' hoga wo poori line hategi.")


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

    # Build the caption transform from this user's rules (if any).
    r = _get_rules(uid)
    if rules_mod.has_any(r):
        transform = lambda t, _r=r: rules_mod.apply_rules(t, _r)
        mode_note = "✏️ Copy mode (rules apply ho rahe hain)"
    else:
        transform = None
        mode_note = "⚡ Forward mode (exact copy)"

    status = await event.respond(f"Clone shuru kar raha hoon...\n{mode_note}")

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
            transform=transform,
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
