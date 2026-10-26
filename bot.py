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
PER_COUNTRY, TOP_ALL, MAX_TEST = 15, 100, 1500
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
        re
