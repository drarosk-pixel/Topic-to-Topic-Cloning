"""
Forum (topic group) cloning engine.

Strategy
--------
Everything is done SERVER-SIDE. No media is downloaded or re-uploaded:
messages are copied using messages.forwardMessages with drop_author=True,
which produces a clean copy (no "Forwarded from" header) while re-using the
file references already on Telegram's servers. This only works when the
source group allows forwarding (content is not protected), which is the
user's case.

Flood control
-------------
Every network call goes through `_flood` which transparently waits out any
FloodWaitError and retries, so the clone never hard-fails on rate limits.
Small deliberate sleeps between batches keep us well under the limits.
"""

import asyncio
import inspect

from telethon import helpers
from telethon.tl import functions, types
from telethon.errors import FloodWaitError, RPCError


def _resolve_request(module, *candidate_names):
    """Find a raw request class by name, searching the whole functions tree.

    Telethon renames/moves generated classes between versions. We first try
    the given module, then every submodule under telethon.tl.functions. If
    still not found, we raise a clear error with the installed Telethon
    version and the Forum-related names we actually see, so the problem can
    be pinpointed (usually a broken/incomplete Telethon install).
    """
    for name in candidate_names:
        cls = getattr(module, name, None)
        if cls is not None:
            return cls

    # Fallback: scan every submodule of telethon.tl.functions
    import importlib
    import pkgutil
    import telethon.tl.functions as _F
    for _finder, modname, _ispkg in pkgutil.iter_modules(_F.__path__):
        try:
            sub = importlib.import_module(f"telethon.tl.functions.{modname}")
        except Exception:
            continue
        for name in candidate_names:
            cls = getattr(sub, name, None)
            if cls is not None:
                return cls

    # Still nothing -> report version + what forum classes exist anywhere.
    import telethon
    found = set()
    for _finder, modname, _ispkg in pkgutil.iter_modules(_F.__path__):
        try:
            sub = importlib.import_module(f"telethon.tl.functions.{modname}")
        except Exception:
            continue
        for a in dir(sub):
            if "Forum" in a or "Topic" in a:
                found.add(f"{modname}.{a}")
    raise RuntimeError(
        f"Request class {candidate_names} Telethon me nahi mila. "
        f"Telethon version: {getattr(telethon, '__version__', '?')}. "
        f"Poore functions tree me ye Forum/Topic classes mile: "
        f"{sorted(found) or 'KUCH BHI NAHI'}. "
        f"Iska matlab Telethon adhoora/purana install hai - reinstall karo: "
        f"pip install -U --force-reinstall --no-cache-dir telethon"
    )


def _mk(cls, **kwargs):
    """Build a request, dropping kwargs the installed version doesn't accept.

    Also auto-fills a required `tabs` arg (newer channels.toggleForum) when
    the signature demands it and the caller didn't pass one.
    """
    params = inspect.signature(cls.__init__).parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    if not accepts_var_kw:
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    if "tabs" in params and "tabs" not in kwargs:
        kwargs["tabs"] = False
    return cls(**kwargs)


async def _enable_forum(client, dest):
    """Enable forum/topics on a channel across Telethon layer versions."""
    cls = _resolve_request(functions.channels,
                           "ToggleForumRequest", "ToggleForum")
    await _flood(lambda: client(_mk(cls, channel=dest, enabled=True)))


async def _flood(factory, retries: int = 5):
    """Run an awaitable produced by `factory`, waiting out FloodWait errors.

    `factory` must be a zero-arg callable returning a fresh coroutine each
    time so it can be retried safely.
    """
    attempt = 0
    while True:
        try:
            return await factory()
        except FloodWaitError as e:
            # Respect Telegram's cool-down exactly, plus a small buffer.
            await asyncio.sleep(e.seconds + 3)
        except RPCError:
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(2 * attempt)


def _extract_topic_id(updates) -> int:
    """The id of a newly created forum topic == id of its service message."""
    # Prefer the explicit UpdateMessageID if present.
    for u in getattr(updates, "updates", []):
        if isinstance(u, types.UpdateMessageID):
            return u.id
    for u in getattr(updates, "updates", []):
        if isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            return u.message.id
    raise RuntimeError("Naya topic id nahi mil paya (unexpected update shape).")


async def get_all_topics(client, channel):
    """Return every ForumTopic in `channel`, newest-first (Telegram order)."""
    cls = _resolve_request(functions.channels,
                           "GetForumTopicsRequest", "GetForumTopics")
    topics = []
    offset_date = 0
    offset_id = 0
    offset_topic = 0
    while True:
        res = await _flood(lambda: client(_mk(
            cls,
            channel=channel,
            q=None,
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=100,
        )))
        batch = [t for t in res.topics if isinstance(t, types.ForumTopic)]
        topics.extend(batch)
        if len(res.topics) < 100:
            break
        last = res.topics[-1]
        msg_by_id = {m.id: m for m in res.messages}
        lm = msg_by_id.get(getattr(last, "top_message", 0))
        offset_topic = last.id
        offset_id = getattr(last, "top_message", 0)
        offset_date = lm.date if lm is not None else 0
    return topics


async def _forward_topic_messages(client, source, dest, src_topic_id,
                                  dest_topic_id, sleep_between):
    """Copy all real messages of one source topic into the dest topic.

    Albums (grouped media) are forwarded together so they stay as one album.
    Service messages (topic created / pinned / joined etc.) are skipped.
    Returns the number of messages copied.
    """
    msgs = []
    async for m in client.iter_messages(source, reply_to=src_topic_id, reverse=True):
        if isinstance(m, types.MessageService):
            continue
        if getattr(m, "action", None) is not None:
            continue
        msgs.append(m)

    # General topic -> no top_msg_id (messages land in General by default).
    top = None if dest_topic_id in (None, 1) else dest_topic_id

    copied = 0
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        gid = getattr(m, "grouped_id", None)
        if gid:
            batch = [m]
            j = i + 1
            while j < n and getattr(msgs[j], "grouped_id", None) == gid:
                batch.append(msgs[j])
                j += 1
            ids = [x.id for x in batch]
            i = j
        else:
            ids = [m.id]
            i += 1

        fwd_cls = _resolve_request(functions.messages,
                                   "ForwardMessagesRequest", "ForwardMessages")
        await _flood(lambda ids=ids, top=top, fwd_cls=fwd_cls: client(_mk(
            fwd_cls,
            from_peer=source,
            to_peer=dest,
            id=ids,
            random_id=[helpers.generate_random_long() for _ in ids],
            top_msg_id=top,
            drop_author=True,          # clean copy, no "forwarded from"
            drop_media_captions=False,  # keep captions exactly
        )))
        copied += len(ids)
        await asyncio.sleep(sleep_between)
    return copied


async def clone_forum(client, source_id, progress_cb,
                      sleep_between=0.6, sleep_per_topic=0.5):
    """Clone a whole forum/topic group into a brand new one.

    Parameters
    ----------
    client        : authorized Telethon userbot client
    source_id     : -100... id (or @username) of the source forum group
    progress_cb   : async callable(stage: str, info: dict) for UI updates

    Returns the created destination chat and total messages copied.
    """
    source = await client.get_entity(source_id)

    if not getattr(source, "forum", False):
        raise RuntimeError(
            "Ye group ek forum/topic group nahi lag raha. "
            "Sirf 'Topics' enabled groups copy ho sakte hain."
        )

    full = await _flood(lambda: client(
        functions.channels.GetFullChannelRequest(channel=source)))
    about = (full.full_chat.about or "")[:255]
    title = source.title

    await progress_cb("create", {"title": title})

    # 1) Create the destination supergroup and turn it into a forum.
    res = await _flood(lambda: client(functions.channels.CreateChannelRequest(
        title=title,
        about=about,
        megagroup=True,
    )))
    dest = res.chats[0]
    await _enable_forum(client, dest)

    # 2) Recreate topics (oldest-first so numbering feels natural).
    topics = await get_all_topics(client, source)
    topics = list(reversed(topics))

    create_cls = _resolve_request(
        functions.channels, "CreateForumTopicRequest", "CreateForumTopic")
    edit_cls = _resolve_request(
        functions.channels, "EditForumTopicRequest", "EditForumTopic")

    topic_map = {}  # source topic id -> dest topic id
    skipped = []    # topics we could not fully process
    for t in topics:
        if t.id == 1:
            # General topic already exists in every forum (id 1).
            topic_map[1] = 1
            if t.title and t.title != "General":
                try:
                    await _flood(lambda t=t: client(_mk(
                        edit_cls, channel=dest, topic_id=1, title=t.title)))
                except Exception:
                    pass
            continue

        try:
            created = await _flood(lambda t=t: client(_mk(
                create_cls,
                channel=dest,
                title=t.title,
                icon_color=t.icon_color,
                icon_emoji_id=t.icon_emoji_id,
                random_id=helpers.generate_random_long(),
            )))
            topic_map[t.id] = _extract_topic_id(created)
            await progress_cb("topic", {"title": t.title})
        except Exception as e:
            skipped.append(f"topic banate waqt '{t.title}': {e}")
        await asyncio.sleep(sleep_per_topic)

    # 3) Copy messages topic-by-topic, in chronological order.
    # A failure in one topic never aborts the whole clone.
    copyable = [t for t in topics if t.id in topic_map]
    topics_total = len(copyable)
    total = 0
    done = 0
    for t in copyable:
        dest_topic = topic_map.get(t.id)
        count = 0
        try:
            count = await _forward_topic_messages(
                client, source, dest, t.id, dest_topic, sleep_between)
            total += count
        except Exception as e:
            skipped.append(f"messages copy '{t.title}': {e}")
        done += 1
        await progress_cb("messages", {
            "title": t.title,
            "count": count,
            "total": total,
            "index": done,
            "topics_total": topics_total,
        })

    return dest, total, skipped
