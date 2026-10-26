import os, re, json, base64, asyncio, socket, threading, time, random
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
SECRET = os.environ.get("SUB_SECRET", "change-me")
ADMIN_ID = os.environ.get("ADMIN_ID", "")
BASE = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
SOURCES = [s.strip() for s in re.split(r"[,\n]", os.environ.get("SOURCES", "")) if s.strip()]
PER_COUNTRY, TOP_ALL, MAX_TEST = 15, 100, 4000
DATA = {}


def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def http_get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


LINK_RE = re.compile(r"(?:vmess|vless|trojan|ss)://[^\s<>\"']+")


def extract(text):
    if "://" not in text[:200]:
        try:
            text = b64d(text).decode("utf-8", "ignore")
        except Exception:
            pass
    return LINK_RE.findall(text)


def parse(link):
    try:
        scheme = link.split("://")[0]
        if scheme == "vmess":
            j = json.loads(b64d(link[8:]))
            return j["add"], int(j["port"])
        body = link.split("://", 1)[1].split("#")[0]
        if scheme == "ss" and "@" not in body:
            body = b64d(body.split("?")[0]).decode()
        rest = body.split("@")[-1]
        hp = re.split(r"[/?]", rest)[0]
        host, port = hp.rsplit(":", 1)
        return host.strip("[]"), int(port)
    except Exception:
        return None


def rename(link, name):
    if link.startswith("vmess://"):
        j = json.loads(b64d(link[8:]))
        j["ps"] = name
        return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()
    return link.split("#")[0] + "#" + quote(name)


def flag(cc):
    if len(cc) == 2:
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())
    return "🏳"


async def ping(sem, item):
    link, host, port = item
    async with sem:
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM), 4)
            ip = infos[0][4][0]
            t = time.perf_counter()
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 3)
            ms = int((time.perf_counter() - t) * 1000)
            w.close()
            return {"link": link, "ip": ip, "ms": ms}
        except Exception:
            return None


def geo_batch(ips):
    out = {}
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        try:
            res = json.loads(http_get(
                "http://ip-api.com/batch?fields=query,countryCode,status",
                json.dumps(chunk).encode()))
            for r in res:
                if r.get("status") == "success":
                    out[r["query"]] = r["countryCode"]
        except Exception as e:
            print("geo error", e)
        time.sleep(4.5)
    return out


async def refresh():
    global DATA
    print("refresh started, sources:", len(SOURCES))
    links = set()
    for url in SOURCES:
        try:
            text = await asyncio.to_thread(http_get, url)
            links.update(extract(text))
        except Exception as e:
            print("source error", url, e)
    items, seen = [], set()
    for l in links:
        p = parse(l)
        if p and p not in seen:
            seen.add(p)
            items.append((l, p[0], p[1]))
    random.shuffle(items)
    items = items[:MAX_TEST]
    sem = asyncio.Semaphore(200)
    res = [r for r in await asyncio.gather(*[ping(sem, i) for i in items]) if r]
    res.sort(key=lambda r: r["ms"])
    res = res[:400]
    countries_map = await asyncio.to_thread(geo_batch, list({r["ip"] for r in res}))
    new, allc = {}, []
    for r in res:
        cc = countries_map.get(r["ip"], "XX")
        name = f"{flag(cc)} {cc} | {r['ms']}ms"
        try:
            link = rename(r["link"], name)
        except Exception:
            continue
        if len(new.setdefault(cc, [])) < PER_COUNTRY:
            new[cc].append(link)
        if len(allc) < TOP_ALL:
            allc.append(link)
    new["ALL"] = allc
    DATA = new
    print("refresh done:", {k: len(v) for k, v in DATA.items()})


async def refresh_loop():
    while True:
        try:
            await refresh()
        except Exception as e:
            print("refresh error", e)
        await asyncio.sleep(1800)


class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == ["health"] or parts == [""]:
            body, code = b"ok", 200
        elif len(parts) >= 2 and parts[0] == "sub" and parts[1] == SECRET:
            cc = parts[2].upper() if len(parts) > 2 else "ALL"
            lines = DATA.get(cc, [])
            body, code = base64.b64encode("\n".join(lines).encode()), 200
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("profile-update-interval", "1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def run_web():
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Web).serve_forever()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"لینک سابسکرایب همه کشورها:\n{BASE}/sub/{SECRET}\n\n"
        "لیست کشورها: /countries\nبه‌روزرسانی هر ۳۰ دقیقه انجام می‌شه.")


async def countries(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"{flag(c)} {c} ({len(v)}): {BASE}/sub/{SECRET}/{c}"
             for c, v in sorted(DATA.items()) if c != "ALL" and v]
    if not lines:
        await update.message.reply_text("هنوز داده‌ای آماده نیست. SOURCES رو چک کن و چند دقیقه صبر کن.")
        return
    await update.message.reply_text("\n".join(lines)[:4000])


async def update_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and str(update.effective_user.id) == ADMIN_ID:
        await update.message.reply_text("شروع شد...")
        asyncio.create_task(refresh())


async def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("countries", countries))
    app.add_handler(CommandHandler("update", update_cmd))
    async with app:
        await app.start()
        await app.updater.start_polling()
        asyncio.create_task(refresh_loop())
        await asyncio.Event().wait()


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
