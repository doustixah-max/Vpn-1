import os, re, json, base64, asyncio, socket, threading, time, random, io, tempfile
import urllib.request
from urllib.parse import quote, urlparse, parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

# ───────────────────────── Config ─────────────────────────

TOKEN    = os.environ["BOT_TOKEN"]
SECRET   = os.environ["SUB_SECRET"]
ADMIN_ID = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else 0
BASE     = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
SOURCES  = [s.strip() for s in re.split(r"[,\n]", os.environ.get("SOURCES", "")) if s.strip()]

XRAY_BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
PER_COUNTRY, TOP_ALL, MAX_TEST = 15, 120, 2000
BAD = {"IR"}
DATA = {"all": [], "by_country": {}, "by_protocol": {}, "updated_at": 0}

# ───────────────────────── Helpers ─────────────────────────

def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))

def http_get(url, data=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")

LINK_RE = re.compile(r"(?:vmess|vless|trojan|ss)://[^\s<>\"']+")

def extract(text):
    found = LINK_RE.findall(text)
    if found:
        return found
    try:
        return LINK_RE.findall(b64d(text).decode("utf-8", "ignore"))
    except Exception:
        return []

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
        return "vmess://" + base64.b64encode(json.dumps(j, ensure_ascii=False).encode()).decode()
    return link.split("#")[0] + "#" + quote(name, safe="")

def flag(cc):
    if len(cc) == 2 and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())
    return "🏳"

COUNTRY_NAMES = {
    "US":"آمریکا","DE":"آلمان","NL":"هلند","GB":"انگلیس","FR":"فرانسه",
    "TR":"ترکیه","AE":"امارات","CA":"کانادا","JP":"ژاپن","SG":"سنگاپور",
    "FI":"فنلاند","SE":"سوئد","PL":"لهستان","RU":"روسیه","IN":"هند",
    "HK":"هنگ‌کنگ","KR":"کره جنوبی","BR":"برزیل","IT":"ایتالیا","ES":"اسپانیا",
    "CH":"سوئیس","AT":"اتریش","BE":"بلژیک","CZ":"چک","AU":"استرالیا",
    "IL":"اسرائیل","UA":"اوکراین","RO":"رومانی","LV":"لتونی","LT":"لیتوانی",
    "NO":"نروژ","DK":"دانمارک","IE":"ایرلند","PT":"پرتغال","GR":"یونان",
}

def country_title(cc):
    return f"{flag(cc)} {COUNTRY_NAMES.get(cc, cc)}"

def proto_title(p):
    return {"vless":"🟣 VLESS","vmess":"🔵 VMess","trojan":"🟢 Trojan","ss":"🟡 Shadowsocks"}.get(p, p)

# ───────────────────────── تبدیل URI به JSON (Xray) ─────────────────────────

def _stream_settings(net, qs, host_default):
    net = net or "tcp"
    stream = {"network": net}
    if net == "ws":
        stream["wsSettings"] = {
            "path": unquote(qs.get("path", ["/"])[0]) or "/",
            "headers": {"Host": qs.get("host", [host_default])[0] or host_default},
        }
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": unquote(qs.get("serviceName", qs.get("path", [""]))[0]).lstrip("/")}
    elif net == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": unquote(qs.get("path", ["/"])[0]),
            "host": qs.get("host", [host_default])[0],
        }
    elif net == "tcp" and qs.get("headerType", [""])[0] == "http":
        stream["tcpSettings"] = {"header": {"type": "http"}}
    return stream

def _apply_security(stream, qs, host_default):
    sec = qs.get("security", ["none"])[0]
    if sec in ("tls", "xtls", "reality"):
        sni = qs.get("sni", qs.get("peer", [""]))[0] or host_default
        tls = {"serverName": sni}
        fp = qs.get("fp", [""])[0]
        if fp: tls["fingerprint"] = fp
        alpn = qs.get("alpn", [""])[0]
        if alpn: tls["alpn"] = [a for a in alpn.split(",") if a]
        if sec == "reality":
            stream["security"] = "reality"
            tls["publicKey"] = qs.get("pbk", [""])[0]
            tls["shortId"] = qs.get("sid", [""])[0]
            tls["spiderX"] = qs.get("spx", ["/"])[0]
            stream["realitySettings"] = tls
        else:
            stream["security"] = "tls"
            stream["tlsSettings"] = tls
    return stream

def vless_to_xray(uri):
    u = urlparse(uri); qs = parse_qs(u.query)
    host, port = u.hostname, u.port or 443
    user = {"id": unquote(u.username or ""), "encryption": "none"}
    flow = qs.get("flow", [""])[0]
    if flow: user["flow"] = flow
    stream = _stream_settings(qs.get("type", ["tcp"])[0], qs, host)
    stream = _apply_security(stream, qs, host)
    return {"protocol": "vless", "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]}, "streamSettings": stream}

def vmess_to_xray(uri):
    j = json.loads(b64d(uri[8:]))
    host, port = j["add"], int(j["port"])
    user = {"id": j["id"], "alterId": int(j.get("aid", 0)), "security": j.get("scy", "auto")}
    net = j.get("net", "tcp")
    stream = {"network": net}
    if net == "ws":
        stream["wsSettings"] = {"path": j.get("path", "/") or "/", "headers": {"Host": j.get("host", host) or host}}
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": j.get("path", "").lstrip("/")}
    if j.get("tls") == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {"serverName": j.get("sni", "") or j.get("host", "") or host}
    return {"protocol": "vmess", "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]}, "streamSettings": stream}

def trojan_to_xray(uri):
    u = urlparse(uri); qs = parse_qs(u.query)
    host, port = u.hostname, u.port or 443
    stream = _stream_settings(qs.get("type", ["tcp"])[0], qs, host)
    stream = _apply_security(stream, qs, host)
    return {"protocol": "trojan", "settings": {"servers": [{"address": host, "port": port, "password": unquote(u.username or "")}]}, "streamSettings": stream}

def ss_to_xray(uri):
    body = uri[5:]
    if "@" not in body:
        body = b64d(body.split("#")[0]).decode()
    else:
        head, tail = body.split("@", 1)
        try: mp = b64d(head).decode()
        except Exception: mp = unquote(head)
        body = mp + "@" + tail
    body = body.split("#")[0]
    mp, hp = body.rsplit("@", 1)
    method, password = mp.split(":", 1)
    hp = hp.split("?")[0]
    host, port = hp.rsplit(":", 1)
    return {"protocol": "shadowsocks", "settings": {"servers": [{"address": host.strip("[]"), "port": int(port), "method": method, "password": password}]}, "streamSettings": {"network": "tcp"}}

def uri_to_xray(uri):
    try:
        scheme = uri.split("://", 1)[0].lower()
        if scheme == "vless":   return vless_to_xray(uri)
        if scheme == "vmess":   return vmess_to_xray(uri)
        if scheme == "trojan":  return trojan_to_xray(uri)
        if scheme == "ss":      return ss_to_xray(uri)
    except Exception:
        pass
    return None

# ───────────────────────── تست واقعی با Xray ─────────────────────────

async def _socks_connect(reader, writer, host_b, port):
    writer.write(b"\x05\x01\x00"); await writer.drain()
    if (await reader.readexactly(2)) != b"\x05\x00": raise RuntimeError("socks hello")
    req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + port.to_bytes(2, "big")
    writer.write(req); await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0: raise RuntimeError(f"socks code={head[1]}")
    atyp = head[3]
    if atyp == 1: await reader.readexactly(6)
    elif atyp == 3:
        ln = (await reader.readexactly(1))[0]; await reader.readexactly(ln + 2)
    elif atyp == 4: await reader.readexactly(18)

async def test_one_xray(uri, timeout=8.0):
    outbound = uri_to_xray(uri)
    if not outbound: return None
    port = random.randint(20000, 60000)
    cfg = {
        "log": {"loglevel": "none"},
        "inbounds": [{"port": port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": False, "auth": "noauth"}}],
        "outbounds": [outbound],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg, f); cfg_path = f.name
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            XRAY_BIN, "run", "-c", cfg_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.6)
        t0 = time.perf_counter()
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
        try:
            await asyncio.wait_for(_socks_connect(reader, writer, b"checkip.amazonaws.com", 80), timeout)
            writer.write(b"GET / HTTP/1.1\r\nHost: checkip.amazonaws.com\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(2048), timeout)
            if not data.startswith(b"HTTP/"): return None
            return {"uri": uri, "ms": int((time.perf_counter() - t0) * 1000)}
        finally:
            try: writer.close(); await writer.wait_closed()
            except Exception: pass
    except Exception:
        return None
    finally:
        if proc:
            try: proc.terminate(); await asyncio.wait_for(proc.wait(), 2)
            except Exception:
                try: proc.kill()
                except Exception: pass
        try: os.unlink(cfg_path)
        except Exception: pass

# ───────────────────────── Geo ─────────────────────────

def geo_batch(ips):
    out = {}
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        try:
            res = json.loads(http_get("http://ip-api.com/batch?fields=query,countryCode,status", json.dumps(chunk).encode()))
            for r in res:
                if r.get("status") == "success": out[r["query"]] = r["countryCode"]
        except Exception as e: print("geo error", e)
        time.sleep(4.5)
    return out

# ───────────────────────── Refresh ─────────────────────────

async def refresh():
    global DATA
    print("refresh start, sources:", len(SOURCES))
    links = set()
    for url in SOURCES:
        try:
            text = await asyncio.to_thread(http_get, url)
            links.update(extract(text))
        except Exception as e: print("source error", url, e)

    items, seen = [], set()
    for l in links:
        p = parse(l)
        if not p: continue
        key = l.split("#", 1)[0]
        if key in seen: continue
        seen.add(key)
        items.append(l)

    random.shuffle(items)
    items = items[:MAX_TEST]
    print("testing", len(items))

    sem = asyncio.Semaphore(25)
    async def worker(u):
        async with sem:
            return await test_one_xray(u, timeout=6)

    results = [r for r in await asyncio.gather(*(worker(u) for u in items)) if r]
    results.sort(key=lambda r: r["ms"])
    results = results[:300]
    print("alive:", len(results))

    hosts = set()
    for r in results:
        p = parse(r["uri"])
        if p: hosts.add(p[0])

    cmap = await asyncio.to_thread(geo_batch, list(hosts))

    entries = []
    for r in results:
        p = parse(r["uri"])
        if not p: continue
        cc = cmap.get(p[0], "XX")
        if cc in BAD: continue
        proto = r["uri"].split("://", 1)[0].lower()
        entries.append({"uri": r["uri"], "country": cc, "protocol": proto, "ms": r["ms"], "host": p[0]})

    allc = entries[:TOP_ALL]
    by_country, by_proto = {}, {}
    for e in entries:
        lst = by_country.setdefault(e["country"], [])
        if len(lst) < PER_COUNTRY: lst.append(e)
        lst2 = by_proto.setdefault(e["protocol"], [])
        if len(lst2) < PER_COUNTRY * 3: lst2.append(e)

    DATA = {"all": allc, "by_country": by_country, "by_protocol": by_proto, "updated_at": time.time()}
    print("refresh done:", {k: len(v) for k, v in by_country.items()})

async def refresh_loop():
    while True:
        try: await refresh()
        except Exception as e: print("refresh error", e)
        await asyncio.sleep(1800)

# ───────────────────────── HTTP Subscription Server ─────────────────────────

class SubHandler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path in ("/", "/health", "/healthz"):
                return self._send(200, b"OK")
            if not u.path.startswith(f"/sub/{SECRET}"):
                return self._send(404, b"not found")
            qs = parse_qs(u.query)
            t = qs.get("type", ["all"])[0]
            if t == "country":
                cc = qs.get("cc", [""])[0].upper()
                entries = DATA["by_country"].get(cc, [])
                title = f"Brock VPN - {cc}"
            elif t == "proto":
                p = qs.get("p", [""])[0].lower()
                entries = DATA["by_protocol"].get(p, [])
                title = f"Brock VPN - {p}"
            else:
                entries = DATA["all"]
                title = "Brock VPN"
            lines = []
            for e in entries:
                name = f"{flag(e['country'])} {e['country']} | {e['protocol']} | {e['ms']}ms"
                try: lines.append(rename(e["uri"], name))
                except Exception: continue
            data = base64.b64encode("\n".join(lines).encode()).decode()
            return self._send(200, data.encode(), extra={"Profile-Title": title, "Profile-Update-Interval": "30"})
        except Exception as e:
            print("http err", e)
            try: self._send(500, b"err")
            except Exception: pass

    def log_message(self, *a, **k): pass

def start_http():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), SubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"HTTP listening on :{port}")

def sub_url(qs=""):
    if not BASE: return ""
    return f"{BASE}/sub/{SECRET}{qs}"

# ───────────────────────── Menu ─────────────────────────

HEADER = "<b>✦ Brock VPN ✦</b>\n<i>کانفیگ‌های تایید‌شده و پرسرعت</i>\n"

def _back_row(): return [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:main")]

def build_main_menu():
    total = len(DATA["all"]); countries = len(DATA["by_country"]); protos = len(DATA["by_protocol"])
    kb = [
        [InlineKeyboardButton(f"⚡️ بهترین‌ها ({min(10,total)} کانفیگ)", callback_data="menu:top:10")],
        [InlineKeyboardButton(f"🌍 کشورها ({countries})", callback_data="menu:country"),
         InlineKeyboardButton(f"🛡️ پروتکل‌ها ({protos})", callback_data="menu:proto")],
        [InlineKeyboardButton("📊 آمار زنده", callback_data="menu:stats"),
         InlineKeyboardButton("🔗 لینک ساب", callback_data="menu:sub")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(kb)

def build_country_menu():
    countries = sorted(DATA["by_country"].items(), key=lambda x: -len(x[1]))
    kb, row = [], []
    for cc, lst in countries[:40]:
        row.append(InlineKeyboardButton(f"{country_title(cc)} · {len(lst)}", callback_data=f"menu:cc:{cc}"))
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    kb.append(_back_row())
    return InlineKeyboardMarkup(kb)

def build_proto_menu():
    kb = []
    for p in ("vless", "vmess", "trojan", "ss"):
        if p in DATA["by_protocol"]:
            kb.append([InlineKeyboardButton(f"{proto_title(p)} · {len(DATA['by_protocol'][p])}", callback_data=f"menu:pr:{p}")])
    kb.append(_back_row())
    return InlineKeyboardMarkup(kb)

def build_stats_text():
    now = time.time(); upd = "—"
    if DATA["updated_at"]:
        mins = int((now - DATA["updated_at"]) / 60); upd = f"{mins} دقیقه پیش"
    pings = [e["ms"] for e in DATA["all"]]
    avg = int(sum(pings) / len(pings)) if pings else 0
    return (HEADER + "\n"
            f"┃ 📦 <b>کل کانفیگ‌ها:</b> {len(DATA['all'])}\n"
            f"┃ 🌍 <b>کشورها:</b> {len(DATA['by_country'])}\n"
            f"┃ 🛡️ <b>پروتکل‌ها:</b> {len(DATA['by_protocol'])}\n"
            f"┃ ⚡️ <b>میانگین پینگ:</b> {avg}ms\n"
            f"┃ 🕐 <b>آخرین بروزرسانی:</b> {upd}")

def build_sub_text():
    if not BASE:
        return HEADER + "\n⚠️ <b>BASE تنظیم نشده است</b>\nمتغیر <code>RENDER_EXTERNAL_URL</code> را ست کنید."
    return (HEADER + "\n🔗 <b>لینک‌های ساب دائم</b>\n\n"
            f"📥 <b>همه:</b>\n<code>{sub_url()}</code>\n\n"
            f"🌍 <b>کشور (مثال DE):</b>\n<code>{sub_url('?type=country&cc=DE')}</code>\n\n"
            f"🛡️ <b>پروتکل (مثال vless):</b>\n<code>{sub_url('?type=proto&p=vless')}</code>\n\n"
            "این لینک‌ها را در v2rayN / v2box / Streisand وارد کنید.")

HELP_TEXT = HEADER + """

📌 <b>راهنما</b>
• <b>بهترین‌ها</b>: کانفیگ‌های برتر بر اساس کمترین پینگ
• <b>کشورها</b>: جدا شده بر اساس کشور سرور
• <b>پروتکل‌ها</b>: جدا شده بر اساس VLESS/VMess/Trojan/SS
• <b>لینک ساب</b>: لینک دائم برای اپ‌ها

🛠 <b>اپ‌ها:</b> v2rayN (ویندوز) · v2box/Streisand (iOS) · v2rayNG (اندروید)
"""

# ───────────────────────── Handlers ─────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DATA["all"]:
        await update.message.reply_text(HEADER + "\n⏳ در حال جمع‌آوری و تست…\n<i>لطفاً چند دقیقه بعد دوباره /start را بزنید.</i>", parse_mode=ParseMode.HTML)
        return
    avg = int(sum(e["ms"] for e in DATA["all"]) / len(DATA["all"])) if DATA["all"] else 0
    text = (HEADER + "\n"
            f"🌍 <b>کشورها:</b> {len(DATA['by_country'])}\n"
            f"🚀 <b>کانفیگ‌ها:</b> {len(DATA['all'])}\n"
            f"⚡️ <b>میانگین پینگ:</b> {avg}ms\n\n"
            "از منوی زیر انتخاب کنید 👇")
    await update.message.reply_text(text, reply_markup=build_main_menu(), parse_mode=ParseMode.HTML)

async def _safe_edit(q, text, kb):
    try: await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception: await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def _send_entries_file(q, entries, title):
    if not entries:
        await q.message.reply_text("⚠️ موردی یافت نشد."); return
    lines = []
    for e in entries:
        name = f"{flag(e['country'])} {e['country']} | {e['protocol']} | {e['ms']}ms"
        try: lines.append(rename(e["uri"], name))
        except Exception: continue
    bio = io.BytesIO("\n".join(lines).encode("utf-8"))
    fname = re.sub(r"[^\w\-]+", "_", title)[:40] + ".txt"
    await q.message.reply_document(
        document=InputFile(bio, filename=fname),
        caption=f"📦 <b>{title}</b>\n✅ {len(entries)} کانفیگ\n⏱ میانگین: {int(sum(e['ms'] for e in entries)/len(entries))}ms",
        parse_mode=ParseMode.HTML,
    )

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); d = q.data or ""

    if d == "menu:main":
        return await _safe_edit(q, build_stats_text() + "\n\n👇 انتخاب کنید:", build_main_menu())
    if d == "menu:country":
        if not DATA["by_country"]: return await _safe_edit(q, "⚠️ هنوز کشوری ثبت نشده.", InlineKeyboardMarkup([_back_row()]))
        return await _safe_edit(q, HEADER + "\n🌍 <b>یک کشور انتخاب کنید:</b>", build_country_menu())
    if d == "menu:proto":
        if not DATA["by_protocol"]: return await _safe_edit(q, "⚠️ هنوز پروتکلی ثبت نشده.", InlineKeyboardMarkup([_back_row()]))
        return await _safe_edit(q, HEADER + "\n🛡️ <b>یک پروتکل انتخاب کنید:</b>", build_proto_menu())
    if d == "menu:stats": return await _safe_edit(q, build_stats_text(), InlineKeyboardMarkup([_back_row()]))
    if d == "menu:sub":   return await _safe_edit(q, build_sub_text(), InlineKeyboardMarkup([_back_row()]))
    if d == "menu:help":  return await _safe_edit(q, HELP_TEXT, InlineKeyboardMarkup([_back_row()]))

    if d.startswith("menu:top:"):
        n = int(d.split(":")[2])
        await _send_entries_file(q, DATA["all"][:n], f"Top-{n}"); return

    if d.startswith("menu:cc:"):
        cc = d.split(":")[2]
        entries = DATA["by_country"].get(cc, [])
        await _send_entries_file(q, entries, f"{country_title(cc)}")
        await q.message.reply_text(f"{flag(cc)} <b>{COUNTRY_NAMES.get(cc, cc)}</b>\n✅ {len(entries)} کانفیگ ارسال شد.",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌍 کشورهای دیگر", callback_data="menu:country")], _back_row()]),
                                   parse_mode=ParseMode.HTML)
        return

    if d.startswith("menu:pr:"):
        p = d.split(":")[2]
        entries = DATA["by_protocol"].get(p, [])
        await _send_entries_file(q, entries, f"{p}-configs")
        await q.message.reply_text(f"{proto_title(p)}\n✅ {len(entries)} کانفیگ ارسال شد.",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛡️ پروتکل‌های دیگر", callback_data="menu:proto")], _back_row()]),
                                   parse_mode=ParseMode.HTML)
        return

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = await update.message.reply_text("🔄 در حال رفرش…")
    try:
        await refresh(); await msg.edit_text(f"✅ رفرش شد: {len(DATA['all'])} کانفیگ")
    except Exception as e: await msg.edit_text(f"❌ {e}")

# ───────────────────────── Main ─────────────────────────

async def post_init(app):
    asyncio.create_task(refresh_loop())

def main():
    if not BASE: print("WARN: RENDER_EXTERNAL_URL خالی است.")
    if SECRET in ("", "change-me") or len(SECRET) < 12:
        raise SystemExit("SUB_SECRET ناامن است. یک رشته طولانی تصادفی تنظیم کنید.")
    start_http()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(cb_router, pattern=r"^menu:"))
    print("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
