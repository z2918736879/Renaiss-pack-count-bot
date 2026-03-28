import aiohttp
import asyncio
import json
import re

# ── Gacha 設定 ─────────────────────────────────────────────────────────────────
GACHA_CONFIG = {
    "omega": {
        "url": "https://www.renaiss.xyz/gacha/omega",
        "tiers": ["S", "A", "B", "C"],
        "emoji": "🔴",
        "cardPackId": "6f6885e6-2ee9-434b-96ea-72d51abae029",
        "next_action": "c0dd5ad7146cc09c75a0e68e180028358f610b1294",
    },
    "renacrypt": {
        "url": "https://www.renaiss.xyz/gacha/renacrypt-pack",
        "tiers": ["Legendary", "Epic", "Rare", "Uncommon", "Common"],
        "emoji": "🔮",
        "cardPackId": "a42fe06a-ae67-4f20-a275-6b2668e178fb",
        "next_action": "c0dd5ad7146cc09c75a0e68e180028358f610b1294",
    },
}

PAGE_LIMIT = 30
MAX_RETRIES = 5       # 每頁最多重試次數
RETRY_DELAY = 3       # 一般錯誤重試間隔（秒）
PAGE_DELAY  = 1       # 每頁之間的間隔（秒），降低被限流機率

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# ── Server Action 請求 ─────────────────────────────────────────────────────────

async def fetch_new_pulls(
    gacha_key: str,
    stop_at_timestamp: int = 0,
) -> list:
    """
    只抓 stop_at_timestamp 之後的新 pull。
    pulls 按時間降序排列（最新在前），遇到舊資料就停止分頁。
    stop_at_timestamp=0 代表抓全部歷史。
    """
    cfg = GACHA_CONFIG[gacha_key]
    action_headers = {
        **HEADERS,
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Next-Action": cfg["next_action"],
        "Next-Router-State-Tree": (
            "%5B%22%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%2C%22%22%2Cnull%2Ctrue%5D"
        ),
    }

    new_pulls: list = []
    offset = 0
    done = False

    body_template = {
        "cardPackId": cfg["cardPackId"],
        "limit": PAGE_LIMIT,
    }

    async with aiohttp.ClientSession() as session:
        while not done:
            body = json.dumps([{**body_template, "offset": offset}])
            page = None

            # ── 單頁抓取，失敗自動重試 ──────────────────────────────
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    async with session.post(
                        cfg["url"], headers=action_headers, data=body,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 429:
                            wait = int(resp.headers.get("Retry-After", 60))
                            print(f"[{gacha_key}] 被限流，等待 {wait} 秒後重試 ({attempt}/{MAX_RETRIES})...")
                            await asyncio.sleep(wait)
                            continue  # 同一頁重試
                        if resp.status != 200:
                            print(f"[{gacha_key}] HTTP {resp.status}，重試 ({attempt}/{MAX_RETRIES})...")
                            await asyncio.sleep(RETRY_DELAY)
                            continue  # 同一頁重試
                        raw = await resp.text(encoding="utf-8", errors="replace")
                        page = _parse_rsc_stream(raw)
                        break  # 成功，離開重試迴圈
                except Exception as e:
                    print(f"[{gacha_key}] 請求失敗 offset={offset} ({attempt}/{MAX_RETRIES}): {e}")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)

            # ── 重試耗盡仍失敗，停止分頁 ────────────────────────────
            if page is None:
                print(f"[{gacha_key}] offset={offset} 重試 {MAX_RETRIES} 次仍失敗，停止抓取")
                break

            if not page:
                break  # 空頁，已到底

            for pull in page:
                ts = pull.get("pulledAtTimestamp", 0)
                if stop_at_timestamp and ts < stop_at_timestamp:
                    done = True   # 已到舊資料，停止
                    break
                new_pulls.append(pull)

            fetched = len(new_pulls)
            print(
                f"[{gacha_key}] offset={offset} → {len(page)} 筆"
                + (f"，累計新增 {fetched} 筆" if stop_at_timestamp else f"，累計 {fetched} 筆")
            )

            if done or len(page) < PAGE_LIMIT:
                break

            offset += PAGE_LIMIT
            await asyncio.sleep(PAGE_DELAY)  # 翻頁前稍作等待，避免觸發限流

    return new_pulls


# ── RSC 解析 ───────────────────────────────────────────────────────────────────

def _parse_rsc_stream(text: str) -> list | None:
    for line in text.splitlines():
        m = re.match(r'^\d+:(\[.+)', line.strip())
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if _looks_like_pulls(data):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _looks_like_pulls(lst) -> bool:
    if not isinstance(lst, list) or not lst or not isinstance(lst[0], dict):
        return False
    s = lst[0]
    return "tier" in s and ("id" in s or "pulledAtTimestamp" in s)


# ── 工具函式 ───────────────────────────────────────────────────────────────────

def extract_psa(image_url: str) -> str:
    m = re.search(r'PSA(\d+)', image_url or "")
    return f"PSA{m.group(1)}" if m else "—"


def parse_fmv(fmv_str: str) -> str:
    """'$n4600' → '$46'"""
    try:
        digits = re.sub(r'[^0-9]', '', fmv_str or "")
        val = int(digits)
        return f"${val // 100}" if val >= 100 else f"${val}"
    except Exception:
        return fmv_str or "—"


def count_tiers(pulls: list, tier_list: list[str]) -> dict:
    counts = {t: 0 for t in tier_list}
    unknown = 0
    for pull in pulls:
        raw = str(pull.get("tier", "")).strip()
        matched = False
        for t in tier_list:
            if raw.lower() == t.lower():
                counts[t] += 1
                matched = True
                break
        if not matched:
            unknown += 1
    result = {"total": len(pulls), **counts}
    if unknown:
        result["unknown"] = unknown
    return result
