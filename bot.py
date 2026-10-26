async def countries(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not DATA:
        await update.message.reply_text("هنوز داده‌ای آماده نیست، چند دقیقه دیگه امتحان کن.")
        return
    lines = [f"{flag(c)} {c} ({len(v)}): {BASE}/sub/{SECRET}/{c}"
             for c, v in sorted(DATA.items()) if c != "ALL"]
    await update.message.reply_text("\n".join(lines)[:4000])
