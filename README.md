# Telegram Topic-Group Cloner Bot

Ek Telegram bot jo runtime par `/login` ke through ek **userbot** banata hai
(phone → OTP → optional 2FA) — koi session string environment me daalni nahi
padti. Login ke baad bot aapke **forum/topic group** ko hubohu copy karta hai:
same title, same topics (naam + icon), aur har topic ka content **wahi order**
me.

## Ye kaise copy karta hai

Sab kuch **server-side** hota hai. Koi media download/upload nahi hota —
messages `messages.forwardMessages` se `drop_author=True` ke saath copy hote
hain, jisse clean copy banti hai (bina "Forwarded from" header) aur Telegram ke
server par maujood file references reuse ho jaate hain.

> Iske liye source group me **forwarding on** honi chahiye (content protected
> nahi hona chahiye). Aapka case yahi hai.

FloodWait handle hota hai: har network call ek retry-wrapper se jaati hai jo
Telegram ka cooldown exactly wait karke retry karti hai, isliye clone rate
limit par hard-fail nahi hota.

## Setup

1. **API credentials** lo — https://my.telegram.org → *API development tools* →
   `API_ID` aur `API_HASH`.
2. **Bot token** lo — Telegram par [@BotFather](https://t.me/BotFather) se
   `/newbot` karke `BOT_TOKEN`.
3. Dependencies install karo:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. Env set karo:
   ```bash
   cp .env.example .env   # phir values bharo
   export $(grep -v '^#' .env | xargs)   # ya apne tarike se export karo
   ```
5. Bot chalao:
   ```bash
   python bot.py
   ```

## Istemaal

Apne bot ko Telegram par kholo aur:

1. `/login` → phone number country code ke saath bhejo (`+91...`).
2. OTP **spaces ke saath** bhejo, jaise `1 2 3 4 5`
   (spaces zaroori — plain code bhejne par Telegram use security ke liye
   invalid kar deta hai).
3. Agar 2FA on hai to password bhejo.
4. `/clone` → source forum group ki id bhejo (`-1001234567890`).
   - Aapka userbot us group ka **member** hona chahiye.
5. Bot naya group bana dega aur progress batata rahega.

Baaki commands: `/logout`, `/cancel`, `/help`.

## Files

| File               | Kaam                                             |
|--------------------|--------------------------------------------------|
| `bot.py`           | Bot + login state machine + clone trigger        |
| `cloner.py`        | Forum clone engine (topics + server-side copy)   |
| `session_store.py` | Userbot sessions disk par save/load              |
| `config.py`        | Env se configuration                             |

## Security

- `sessions.json` me userbot ki **poori account access** hoti hai. Ise private
  rakho — `.gitignore` me already hai.
- `.env` aur `*.session` files bhi commit mat karna.

## Limitations

- Sirf **forum/topic groups** (Topics enabled) copy hote hain, normal groups
  nahi.
- Message forwarding/edits ke timestamps original nahi rahte (copy ka apna time
  hota hai) — Telegram API ki limitation hai.
- Bahut bade groups par clone ko time lagega (deliberate delays flood se
  bachने ke liye).
