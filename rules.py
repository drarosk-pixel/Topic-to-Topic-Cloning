"""
Per-user text transformation rules applied to message captions/text while
cloning in "copy mode".

A rules dict looks like::

    {
        "word_replacements":  [[old, new], ...],   # new "" => delete the word
        "link_replacements":  [[old, new], ...],   # substring replace in links
        "remove_all_links":   False,               # de-hyperlink + drop bare URLs
        "remove_link_lines":  False,               # drop whole lines containing a link
        "remove_word_lines":  [word, ...],         # drop whole lines containing a word
        "caption":            "",                  # extra caption text
        "caption_pos":        "bottom",            # "top" | "bottom"
    }

`apply_rules` works on the MARKDOWN form of a message (so [text](url) links,
**bold**, etc. are visible and editable as plain string operations).
"""

import re


DEFAULT_RULES = {
    "word_replacements": [],
    "link_replacements": [],
    "remove_all_links": False,
    "remove_link_lines": False,
    "remove_word_lines": [],
    "caption": "",
    "caption_pos": "bottom",
}

# Markdown inline link: [visible text](http://url)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
# A bare URL / telegram link.
_BARE_LINK = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)\S+",
                        re.IGNORECASE)


def normalize(rules):
    """Return a full rules dict, filling in any missing keys with defaults."""
    out = dict(DEFAULT_RULES)
    out.update({k: v for k, v in (rules or {}).items() if k in DEFAULT_RULES})
    # make sure list/str types are sane
    out["word_replacements"] = [list(p) for p in out["word_replacements"]]
    out["link_replacements"] = [list(p) for p in out["link_replacements"]]
    out["remove_word_lines"] = list(out["remove_word_lines"])
    if out["caption_pos"] not in ("top", "bottom"):
        out["caption_pos"] = "bottom"
    return out


def has_any(rules):
    """True if any rule would change the text (i.e. use copy mode)."""
    r = normalize(rules)
    return bool(
        r["word_replacements"] or r["link_replacements"] or r["remove_all_links"]
        or r["remove_link_lines"] or r["remove_word_lines"] or r["caption"]
    )


def _line_has_link(line):
    return bool(_MD_LINK.search(line) or _BARE_LINK.search(line))


def apply_rules(text, rules):
    """Apply the rules to a markdown-formatted caption/text string."""
    r = normalize(rules)
    text = text or ""

    # 1) Line removal (hidden links behind text are removed with the line).
    if r["remove_link_lines"] or r["remove_word_lines"]:
        words = [w.lower() for w in r["remove_word_lines"] if w]
        kept = []
        for line in text.split("\n"):
            if r["remove_link_lines"] and _line_has_link(line):
                continue
            low = line.lower()
            if words and any(w in low for w in words):
                continue
            kept.append(line)
        text = "\n".join(kept)

    # 2) Link replacements (operate on URLs inside markdown links and bare URLs).
    for old, new in r["link_replacements"]:
        if old:
            text = text.replace(old, new)

    # 3) Remove all hyperlinks: keep the visible text of [text](url), then
    #    delete any remaining bare URLs.
    if r["remove_all_links"]:
        text = _MD_LINK.sub(r"\1", text)
        text = _BARE_LINK.sub("", text)

    # 4) Word replace / remove.
    for old, new in r["word_replacements"]:
        if old:
            text = text.replace(old, new)

    # 5) Collapse blank lines left behind, trim trailing spaces per line.
    lines = [ln.rstrip() for ln in text.split("\n")]
    # drop 3+ consecutive blank lines down to one
    cleaned = []
    blank = 0
    for ln in lines:
        if ln.strip() == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        cleaned.append(ln)
    text = "\n".join(cleaned).strip()

    # 6) Add caption.
    if r["caption"]:
        if r["caption_pos"] == "top":
            text = r["caption"] + ("\n\n" + text if text else "")
        else:
            text = (text + "\n\n" if text else "") + r["caption"]

    return text
