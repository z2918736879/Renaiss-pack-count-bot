import os
import asyncio
from datetime import datetime, timezone, timedelta

TZ_UTC8 = timezone(timedelta(hours=8))
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from gacha_fetcher import fetch_new_pulls, extract_psa, parse_fmv, GACHA_CONFIG
from state_manager import load_state, save_state, delete_state

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

POLL_INTERVAL = 15  # 秒（太短容易被網站限流）
LOOKBACK_SECONDS = 120  # 重抓過去 N 秒，確保 API 延遲的資料不漏抓

# ── Tier Emoji ─────────────────────────────────────────────────────────────────
TIER_EMOJI = {
    "s": "⭐", "a": "🔴", "b": "🟠", "c": "🟡",
    "legendary": "🌟", "epic": "🟣", "rare": "🔵",
    "uncommon": "🟢", "common": "⚪",
}

# 出現時需要特別通知的稀有 Tier
ALERT_TIERS = {"s", "legendary"}

# 需要追蹤兩次間距的特殊 Tier（依卡機）
SPECIAL_TIERS = {
    "omega":     ["S", "A"],
    "renacrypt": ["Legendary", "Epic"],
}

def tier_emoji(tier: str) -> str:
    return TIER_EMOJI.get(tier.lower(), "▪️")


# ── 狀態管理 ───────────────────────────────────────────────────────────────────
# _state[chat_id][gacha_key] = {
#   "tier_counts"      : dict  — 累計各 Tier 數量（從 cutoff_timestamp 起）
#   "last_timestamp"   : int   — 最新 pull 的時間戳（用來只抓新資料）
#   "cutoff_timestamp" : int   — 只計算此時間之後的 pull（0=全部）
#   "seen_ids"         : set   — 已處理的 pull ID（避免重複）
#   "new_pulls"        : list  — 本輪新增的 pull（顯示用）
#   "message_id"       : int   — 要持續 edit 的 TG 訊息 ID
#   "initialized"      : bool  — 是否完成初始化
# }

_state: dict = {}


def _init_gacha_state(gacha_key: str, saved: dict = None) -> dict:
    s = saved or {}
    # 恢復特殊 Tier 間隔追蹤（若有存檔）
    saved_gt = s.get("gap_tracker", {})
    gap_tracker = {
        st: {
            "since_last":    saved_gt.get(st, {}).get("since_last", 0),
            "last_interval": saved_gt.get(st, {}).get("last_interval"),
            "count":         saved_gt.get(st, {}).get("count", 0),
        }
        for st in SPECIAL_TIERS.get(gacha_key, [])
    }
    return {
        "tier_counts":      s.get("tier_counts", {}),
        "last_timestamp":   s.get("last_timestamp", 0),
        "cutoff_timestamp": s.get("cutoff_timestamp", 0),
        "seen_ids":         set(s.get("seen_ids", [])),   # 從存檔恢復，避免重啟後重複計算
        "recent_pulls":     s.get("recent_pulls", []),
        "gap_tracker":      gap_tracker,
        "new_pulls":        [],
        "message_id":       s.get("message_id"),
        "initialized":      bool(s.get("tier_counts")),   # 有存檔就算初始化過
    }


def _get(chat_id: int, gacha_key: str) -> dict:
    if chat_id not in _state:
        _state[chat_id] = {}
    if gacha_key not in _state[chat_id]:
        _state[chat_id][gacha_key] = _init_gacha_state(gacha_key)
    return _state[chat_id][gacha_key]


def _save(chat_id: int):
    if chat_id not in _state:
        return
    save_state(chat_id, _state[chat_id])


def _update_gap_and_recent(gacha_key: str, gs: dict, pulls_oldest_first: list):
    """
    以時間升序處理 pulls，更新：
    - gap_tracker：各特殊 Tier 距上次張數 & 上兩次之間隔
    - recent_pulls：保留最新 5 張（newest first）
    """
    gt = gs["gap_tracker"]
    special_tiers = SPECIAL_TIERS.get(gacha_key, [])

    for p in pulls_oldest_first:
        tier = p.get("tier", "")
        for st in special_tiers:
            if tier.lower() == st.lower():
                # 命中特殊 Tier：若已出現過，記錄本次與上次的間距
                if gt[st]["count"] >= 1:
                    gt[st]["last_interval"] = gt[st]["since_last"]
                gt[st]["since_last"] = 0
                gt[st]["count"] += 1
            else:
                gt[st]["since_last"] += 1

    # 新的 pulls 放在最前（newest first），保留最新 5 張
    gs["recent_pulls"] = (list(reversed(pulls_oldest_first)) + gs["recent_pulls"])[:5]


# ── 訊息建構 ───────────────────────────────────────────────────────────────────

def _fmt_time(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=TZ_UTC8).strftime("%H:%M:%S")
    except Exception:
        return "—"


def _fmt_ts_label(ts: int) -> str:
    if not ts:
        return "全部歷史"
    try:
        return datetime.fromtimestamp(ts, tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M (UTC+8)")
    except Exception:
        return "—"


def build_message(gacha_key: str, gs: dict) -> str:
    cfg = GACHA_CONFIG[gacha_key]
    tiers = cfg["tiers"]
    counts = gs["tier_counts"]
    new_pulls = gs["new_pulls"]
    recent_pulls = gs.get("recent_pulls") or []
    total = sum(counts.get(t, 0) for t in tiers)
    new_count = len(new_pulls)

    lines = []

    # 標題
    if new_count > 0:
        lines.append(f'{cfg["emoji"]} <b>{gacha_key.upper()}  +{new_count} 張新卡</b>')
    else:
        lines.append(f'{cfg["emoji"]} <b>{gacha_key.upper()}</b>  <i>（監控中...）</i>')
    lines.append("")

    # 本次新增摘要
    if new_pulls:
        delta = defaultdict(int)
        for p in new_pulls:
            delta[p["tier"].upper()] += 1
        summary = "  ".join(
            f"{tier_emoji(t)} Tier {t} ×{delta[t.upper()]}"
            for t in tiers if delta.get(t.upper(), 0) > 0
        )
        lines.append(f"本次新增：{summary}")
        lines.append("")

    # 最近 5 張記錄（持續顯示，無論有無新卡）
    if recent_pulls:
        lines.append("📋 <b>最近記錄</b>")
        for p in recent_pulls[:5]:
            t   = p.get("tier", "?")
            fmv = parse_fmv(p.get("fmv", ""))
            psa = extract_psa(p.get("frontImageUrl", ""))
            ts  = _fmt_time(p.get("pulledAtTimestamp", 0))
            lines.append(
                f"{tier_emoji(t)} <b>Tier {t.upper()}</b> — {fmv}"
                f"   <code>{psa}</code>   <i>{ts}</i>"
            )
        lines.append("")

    # 特殊 Tier 間隔
    gt = gs.get("gap_tracker", {})
    special_tiers = SPECIAL_TIERS.get(gacha_key, [])
    if gt and special_tiers:
        lines.append("📏 <b>特殊卡間隔</b>")
        for st in special_tiers:
            info = gt.get(st, {})
            count    = info.get("count", 0)
            since    = info.get("since_last", 0)
            last_iv  = info.get("last_interval")
            if count == 0:
                lines.append(f"{tier_emoji(st)} {st} Tier　尚未出現")
            else:
                iv_str = f"<b>{last_iv}</b> 張" if last_iv is not None else "—"
                lines.append(
                    f"{tier_emoji(st)} {st} Tier　"
                    f"距上次 <b>{since}</b> 張　｜　兩次間隔 {iv_str}"
                )
        lines.append("")

    # 累計統計
    cutoff_label = _fmt_ts_label(gs.get("cutoff_timestamp", 0))
    lines.append(f"📊 <b>累計抽取統計（共 {total} 張）</b>")
    lines.append(f"<i>計算起點：{cutoff_label}</i>")

    delta = defaultdict(int)
    for p in new_pulls:
        delta[p["tier"].upper()] += 1

    for t in tiers:
        cnt = counts.get(t, 0)
        d   = delta.get(t.upper(), 0)
        delta_str = f"  <b>+{d}</b>" if d > 0 else ""
        lines.append(f"{tier_emoji(t)} Tier {t:<14} 累計 <b>{cnt}</b> 張{delta_str}")

    lines.append("")
    lines.append(f'🔗 <a href="{cfg["url"]}">查看完整開包記錄</a>')
    return "\n".join(lines)


# ── 輪詢核心 ───────────────────────────────────────────────────────────────────

async def _poll_one(bot, chat_id: int, gacha_key: str):
    gs  = _get(chat_id, gacha_key)
    cfg = GACHA_CONFIG[gacha_key]

    # 初始化：若沒有存檔，第一次抓全部歷史（或從 cutoff 起）
    if not gs["initialized"]:
        print(f"[{gacha_key}] 初始化，抓取歷史資料...")
        all_pulls = await fetch_new_pulls(gacha_key, stop_at_timestamp=gs["cutoff_timestamp"])

        # 建立基準計數
        for t in cfg["tiers"]:
            gs["tier_counts"][t] = 0
        for p in all_pulls:
            for t in cfg["tiers"]:
                if p.get("tier", "").lower() == t.lower():
                    gs["tier_counts"][t] += 1
                    break
            gs["seen_ids"].add(p.get("id", ""))

        gs["last_timestamp"] = all_pulls[0]["pulledAtTimestamp"] if all_pulls else gs["cutoff_timestamp"]
        gs["new_pulls"]      = []
        gs["initialized"]    = True

        # 建立特殊 Tier 間隔追蹤 & 最近 5 張記錄（oldest-first 處理）
        _update_gap_and_recent(gacha_key, gs, list(reversed(all_pulls)))
        gs["_init_complete"] = True  # 通知下一輪 poll 強制更新訊息

        print(f"[{gacha_key}] 初始化完成，共 {sum(gs['tier_counts'].values())} 筆")
        _save(chat_id)

    else:
        # 只抓比 last_timestamp 更新的 pull，且不得早於 cutoff_timestamp
        stop_ts = max(gs["last_timestamp"] - LOOKBACK_SECONDS, gs.get("cutoff_timestamp", 0))
        new_pulls = await fetch_new_pulls(gacha_key, stop_at_timestamp=stop_ts)

        # 過濾已見過的（避免重複）
        new_pulls = [p for p in new_pulls if p.get("id") not in gs["seen_ids"]]

        if new_pulls:
            new_pulls.sort(key=lambda p: p.get("pulledAtTimestamp", 0), reverse=True)
            for p in new_pulls:
                gs["seen_ids"].add(p.get("id", ""))
                for t in cfg["tiers"]:
                    if p.get("tier", "").lower() == t.lower():
                        gs["tier_counts"][t] = gs["tier_counts"].get(t, 0) + 1
                        break
            gs["last_timestamp"] = new_pulls[0]["pulledAtTimestamp"]
            gs["new_pulls"] = new_pulls

            # 更新特殊 Tier 間隔追蹤 & 最近 5 張記錄（oldest-first 處理）
            _update_gap_and_recent(gacha_key, gs, list(reversed(new_pulls)))

            _save(chat_id)
        else:
            gs["new_pulls"] = []

    # 發送或更新 TG 訊息
    text = build_message(gacha_key, gs)
    try:
        if gs["message_id"] is None:
            msg = await bot.send_message(
                chat_id, text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            gs["message_id"] = msg.message_id
            _save(chat_id)
        elif gs["new_pulls"] or gs.pop("_init_complete", False):
            await bot.edit_message_text(
                text, chat_id, gs["message_id"],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except TelegramError as e:
        if "message is not modified" not in str(e):
            print(f"[{gacha_key}] TG Error: {e}")


async def _poll_all(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await asyncio.gather(
        _poll_one(context.bot, chat_id, "omega"),
        _poll_one(context.bot, chat_id, "renacrypt"),
    )


# ── 指令 ──────────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # 停止舊 job
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()

    # 載入存檔（若有）
    saved = load_state(chat_id)
    _state[chat_id] = {
        key: _init_gacha_state(key, saved.get(key))
        for key in ("omega", "renacrypt")
    }

    # 重置 message_id：讓第一次 poll 強制發送新看板，而非等到有新卡才顯示
    if saved:
        for key in ("omega", "renacrypt"):
            _state[chat_id][key]["message_id"] = None

    if saved:
        omega_total     = sum(_state[chat_id]["omega"]["tier_counts"].values())
        renacrypt_total = sum(_state[chat_id]["renacrypt"]["tier_counts"].values())
        await update.message.reply_text(
            "✅ <b>已載入上次存檔！</b>\n\n"
            f"⚡ Omega：{omega_total} 筆\n"
            f"🔮 Renacrypt：{renacrypt_total} 筆\n\n"
            f"繼續從上次中斷點監控，每 {POLL_INTERVAL} 秒更新一次。\n"
            "使用 /stop 停止　/setstart 重設起點",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "🎰 <b>Gacha Pack Count Bot 啟動！</b>\n\n"
            f"首次啟動，正在抓取歷史資料...\n"
            f"之後每 {POLL_INTERVAL} 秒自動追蹤。\n\n"
            "使用 /stop 停止　/setstart 重設起點",
            parse_mode=ParseMode.HTML,
        )

    context.job_queue.run_repeating(
        _poll_all,
        interval=POLL_INTERVAL,
        first=1,
        name=str(chat_id),
        data={"chat_id": chat_id},
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    if jobs:
        for job in jobs:
            job.schedule_removal()
        await update.message.reply_text(
            "⏹ 已停止監控。狀態已儲存，下次 /start 會從這裡繼續。"
        )
    else:
        await update.message.reply_text("目前沒有在監控。使用 /start 開始。")


async def setstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setstart                        — 從現在開始
    /setstart 2026-03-27             — 指定日期（當天 00:00 UTC+8）
    /setstart 2026-03-27 15:30       — 指定日期 + 時間（UTC+8）
    /setstart all                    — 計算全部歷史
    """
    chat_id = update.effective_chat.id
    args    = context.args

    if not args or args[0].lower() == "now":
        cutoff = int(datetime.now(tz=TZ_UTC8).timestamp())
        label  = datetime.now(tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M (UTC+8)")

    elif args[0].lower() == "all":
        cutoff = 0
        label  = "全部歷史"

    else:
        # 組合參數（允許 "2026-03-27 15:30" 分成兩個 args）
        raw = " ".join(args)
        dt  = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            await update.message.reply_text(
                "❌ 格式錯誤，支援以下格式：\n\n"
                "<code>/setstart now</code>  — 從現在起\n"
                "<code>/setstart 2026-03-27</code>  — 指定日期\n"
                "<code>/setstart 2026-03-27 15:30</code>  — 精確到分鐘\n"
                "<code>/setstart all</code>  — 全部歷史\n\n"
                "<i>時間以 UTC+8 為準</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        # 以 UTC+8 解析
        dt_utc8 = dt.replace(tzinfo=TZ_UTC8)
        cutoff  = int(dt_utc8.timestamp())
        label   = dt_utc8.strftime("%Y-%m-%d %H:%M (UTC+8)")

    # 清除舊的 TG 訊息，避免和新訊息混淆
    if chat_id in _state:
        for gacha_key in ("omega", "renacrypt"):
            old_msg_id = _state[chat_id].get(gacha_key, {}).get("message_id")
            if old_msg_id:
                try:
                    await context.bot.edit_message_text(
                        f"🔄 起點已重設，請查看最新訊息。",
                        chat_id, old_msg_id,
                    )
                except TelegramError:
                    pass

    # 重置狀態
    _state.pop(chat_id, None)
    delete_state(chat_id)
    _state[chat_id] = {
        key: _init_gacha_state(key, {"cutoff_timestamp": cutoff})
        for key in ("omega", "renacrypt")
    }

    # 重啟 job
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    context.job_queue.run_repeating(
        _poll_all,
        interval=POLL_INTERVAL,
        first=1,
        name=str(chat_id),
        data={"chat_id": chat_id},
    )

    await update.message.reply_text(
        f"🔄 <b>已重設起點：{label}</b>\n\n"
        "正在重新抓取資料，請稍候...",
        parse_mode=ParseMode.HTML,
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示確認按鈕，防止誤觸。"""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 確認清除", callback_data="confirm_reset"),
        InlineKeyboardButton("❌ 取消",     callback_data="cancel_reset"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>確定要清除所有儲存的資料嗎？</b>\n\n"
        "這將會：\n"
        "• 刪除 state.json\n"
        "• 清空兩個卡機的累計數據\n"
        "• 重新從頭抓取\n\n"
        "此操作<b>無法復原</b>。",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "confirm_reset":
        # 停止 job
        for job in context.job_queue.get_jobs_by_name(str(chat_id)):
            job.schedule_removal()

        # 清除舊的 TG 訊息
        if chat_id in _state:
            for gacha_key in ("omega", "renacrypt"):
                old_msg_id = _state[chat_id].get(gacha_key, {}).get("message_id")
                if old_msg_id:
                    try:
                        await context.bot.delete_message(chat_id, old_msg_id)
                    except TelegramError:
                        pass

        # 清除狀態
        _state.pop(chat_id, None)
        delete_state(chat_id)

        await query.edit_message_text(
            "🗑 <b>資料已清除！</b>\n\n"
            "使用 /start 重新開始監控。",
            parse_mode=ParseMode.HTML,
        )

    elif query.data == "cancel_reset":
        await query.edit_message_text("✅ 已取消，資料保留。")


# ── 啟動 ──────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        print("❌  請在 .env 檔案中設定 TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",    start_cmd))
    app.add_handler(CommandHandler("stop",     stop_cmd))
    app.add_handler(CommandHandler("setstart", setstart_cmd))
    app.add_handler(CommandHandler("reset",    reset_cmd))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern="^(confirm|cancel)_reset$"))

    print(f"✅  Bot 已啟動（每 {POLL_INTERVAL} 秒輪詢），Ctrl+C 停止")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()