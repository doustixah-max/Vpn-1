async def countries(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"{flag(c)} {c} ({len(v)}): {BASE}/sub/{SECRET}/{c}"
             for c, v in sorted(DATA.items()) if c != "ALL" and v]
    if not lines:
        await update.message.reply_text("هنوز داده‌ای آماده نیست. SOURCES رو چک کن و چند دقیقه صبر کن.")
        return
    await update.message.reply_text("\n".join(lines)[:4000])
