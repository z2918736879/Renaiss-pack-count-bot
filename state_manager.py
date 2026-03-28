"""
儲存和載入 Bot 狀態，讓重啟後不需要重新抓取歷史資料。
state.json 結構（以 chat_id 為 key，支援多用戶）：
{
  "111111": {
    "omega": {
      "tier_counts": {"S": 1, "A": 30, "B": 101, "C": 535},
      "last_timestamp": 1774624974,
      "cutoff_timestamp": 0,
      "message_id": null
    },
    "renacrypt": { ... }
  },
  "222222": { ... }
}
"""

import json
import os

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def _load_all() -> dict:
    """讀取整個 state.json，回傳 {chat_id_str: {gacha_key: ...}} 的完整結構。"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_all(all_state: dict):
    """將完整結構寫回 state.json。"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[state] 儲存失敗: {e}")


def load_state(chat_id: int) -> dict:
    """載入指定 chat_id 的狀態，回傳 {gacha_key: ...}，找不到則回傳 {}。"""
    return _load_all().get(str(chat_id), {})


def save_state(chat_id: int, state: dict):
    """儲存指定 chat_id 的狀態，不影響其他用戶的資料。"""
    try:
        all_state = _load_all()
        serializable = {}
        for gacha_key, gs in state.items():
            serializable[gacha_key] = {
                "tier_counts":      gs.get("tier_counts", {}),
                "last_timestamp":   gs.get("last_timestamp", 0),
                "cutoff_timestamp": gs.get("cutoff_timestamp", 0),
                "message_id":       gs.get("message_id"),
                "seen_ids":         list(gs.get("seen_ids", set())),
                "recent_pulls":     gs.get("recent_pulls", []),
                "gap_tracker":      gs.get("gap_tracker", {}),
            }
        all_state[str(chat_id)] = serializable
        _save_all(all_state)
    except Exception as e:
        print(f"[state] 儲存失敗 (chat_id={chat_id}): {e}")


def delete_state(chat_id: int):
    """刪除指定 chat_id 的狀態，不影響其他用戶的資料。"""
    all_state = _load_all()
    if str(chat_id) in all_state:
        del all_state[str(chat_id)]
        _save_all(all_state)
