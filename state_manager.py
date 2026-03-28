"""
儲存和載入 Bot 狀態，讓重啟後不需要重新抓取歷史資料。
state.json 結構：
{
  "omega": {
    "tier_counts": {"S": 1, "A": 30, "B": 101, "C": 535},
    "last_timestamp": 1774624974,   ← 最新一筆 pull 的時間戳
    "cutoff_timestamp": 0,          ← 只計算此時間之後的 pull（0 = 全部）
    "message_id": null
  },
  "renacrypt": { ... }
}
"""

import json
import os

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    try:
        # 只儲存可序列化的部分（排除 seen_ids set）
        serializable = {}
        for gacha_key, gs in state.items():
            serializable[gacha_key] = {
                "tier_counts":       gs.get("tier_counts", {}),
                "last_timestamp":    gs.get("last_timestamp", 0),
                "cutoff_timestamp":  gs.get("cutoff_timestamp", 0),
                "message_id":        gs.get("message_id"),
                "seen_ids":          list(gs.get("seen_ids", set())),
                "recent_pulls":      gs.get("recent_pulls", []),
                "gap_tracker":       gs.get("gap_tracker", {}),
            }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[state] 儲存失敗: {e}")


def delete_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
