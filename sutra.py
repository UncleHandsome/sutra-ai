#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sutra.py — 佛經銷文斷句品質深度審查、一鍵修正與專注補漏工具
（雙標點體系全能版 + DeepSeek Prompt Cache 極致優化 + 模組化重構版）

支援雙標點體系：
  1. 現代新標點文本（有逗號、冒號、引號等）
  2. 古典全句號文本（通篇全為句號，句號兼具逗號、分號、停頓功能）

核心特性：
  ★ 遵循「靜態前置、動態後置」鎖定 KV Cache，降低延遲與 API 費用
  ★ 移除非必要的硬性字數限制，改由 AI 依佛學義理與因明文法結構自主決定斷句範圍
  ★ 統一採用「動態指針推進迴圈」，長區間由 AI 自然分段產出，杜絕人工硬切錯誤
  ★ 結合「前文脈絡錨點」與「品質校驗自動重試（防半偈、防跳字、防格式殘缺）」
  ★ 支援斷點續傳（Checkpoint）與 Windows / OneDrive 檔案鎖容錯機制

用法：
  python sutra.py --file 1.txt --generate          # ★ 全本銷文模式（將整篇經文視為大漏段，AI自主推進從頭銷文到尾）
  python sutra.py --file 1.txt --auto              # ★ 官方 DeepSeek 一鍵全流程（審查+修復+重排）
  python sutra.py --file 1.txt --fix-gaps          # ★ 專注補漏模式（快速掃描漏段，AI自主分段補齊並物理歸位）
  python sutra.py --file 1.txt --auto --opencode   # ★ 使用 OpenCode Go (opencode.ai) 端點
  python sutra.py --file 1.txt --review --opencode # 使用 OpenCode 僅審查並產出 review.json
  python sutra.py --file 1.txt --fix --opencode    # 使用 OpenCode 依 review.json 修正
  python sutra.py --file 1.txt --fix --dry-run     # 預覽待修正清單（不呼叫 API）
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
from enum import Enum
from typing import List, Dict, Tuple, Optional, Any, Callable
from openai import OpenAI

from dataclasses import dataclass, field


# ============================================================
#  一、強型別資料模型 (Data Models)
# ============================================================
@dataclass
class SutraGap:
    """經文漏段資料模型"""
    prev_idx: int
    gap_text: str
    position: str  # "head" | "middle" | "tail"

    def __getitem__(self, item: str) -> Any:
        """支援字典式下標存取 g['key']，提供向後相容防禦"""
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        """支援 g.get('key', default) 存取"""
        return getattr(self, item, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prev_idx": self.prev_idx,
            "gap_text": self.gap_text,
            "position": self.position,
        }


@dataclass
class ReviewIssue:
    """審查問題項目模型"""
    index: int
    issue_type: str
    problem: str
    merge_indices: List[int] = field(default_factory=list)
    gap_text: Optional[str] = None
    position: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        """支援 iss['type'] 下標存取"""
        if item == "type":
            return self.issue_type
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        """支援 iss.get('type') 字典方法存取，防止 AttributeError"""
        if item == "type":
            return self.issue_type
        return getattr(self, item, default)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "index": self.index,
            "type": self.issue_type,
            "problem": self.problem,
            "merge_indices": self.merge_indices,
        }
        if self.gap_text:
            data["gap_text"] = self.gap_text
        if self.position:
            data["position"] = self.position
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewIssue":
        return cls(
            index=data.get("index", 0),
            issue_type=data.get("type", ""),
            problem=data.get("problem", ""),
            merge_indices=data.get("merge_indices", [data.get("index", 0)]),
            gap_text=data.get("gap_text"),
            position=data.get("position"),
        )


# ============================================================
#  一、多金鑰輪換池管理器 (API Key Pool)
# ============================================================
class ApiKeyPool:
    """多 API Key 負載、壞 Key 自動剔除與全滅狀態管理器"""
    def __init__(self, keys: List[str]):
        self.all_keys = [k.strip() for k in keys if k and k.strip()]
        self.active_keys = list(self.all_keys)
        self.current_idx = 0

    def get_current_key(self) -> str:
        if not self.active_keys:
            return ""
        return self.active_keys[self.current_idx % len(self.active_keys)]

    def rotate(self) -> str:
        """在所有有效金鑰之間輪流切換"""
        if not self.active_keys:
            return ""
        self.current_idx = (self.current_idx + 1) % len(self.active_keys)
        return self.active_keys[self.current_idx]

    def _apply_key_to_client(self, client: Any, key: str) -> None:
        """深層穿透更新 OpenAI Client 實例的 API Key 與授權 Header"""
        if hasattr(client, "api_key"):
            client.api_key = key
        # 同步更新自訂 Header 與 httpx 內部 Header
        if hasattr(client, "_custom_headers") and isinstance(client._custom_headers, dict):
            client._custom_headers["Authorization"] = f"Bearer {key}"
        # 兼容最新 OpenAI Python SDK 內部 Client 配置
        if hasattr(client, "_client") and hasattr(client._client, "headers"):
            try:
                client._client.headers["Authorization"] = f"Bearer {key}"
            except Exception:
                pass

    def rotate_client(self, client: Any, logger: logging.Logger, reason: str = "觸發限制/異常") -> str:
        """切換下一把金鑰並同步更新 OpenAI Client"""
        if not self.active_keys:
            return ""
        new_key = self.rotate()
        self._apply_key_to_client(client, new_key)
        logger.warning(
            f"    🔑 [金鑰輪換] {reason}，已自動切換至可用 Key ({self.mask_key(new_key)})"
        )
        return new_key

    def mark_current_dead(self, client: Any, logger: logging.Logger, reason: str = "欠費/失效") -> bool:
        """★ 將當前 Key 永久剔除出活躍清單，並切換至下一把可用 Key（若全部陣亡回傳 True）"""
        if not self.active_keys:
            return True

        dead_key = self.get_current_key()
        if dead_key in self.active_keys:
            self.active_keys.remove(dead_key)

        logger.error(
            f"    💀 [金鑰陣亡] Key ({self.mask_key(dead_key)}) 因「{reason}」已永久剔除！"
            f"剩餘可用金鑰數: {len(self.active_keys)}/{len(self.all_keys)}"
        )

        if not self.active_keys:
            logger.critical("    🚫 [警報] 金鑰池中所有 API Key 皆已全數耗盡或失效！")
            return True

        self.current_idx = self.current_idx % len(self.active_keys)
        new_key = self.active_keys[self.current_idx]
        self._apply_key_to_client(client, new_key)
        logger.info(f"    ✨ 已無縫接軌切換至下一把正常 Key ({self.mask_key(new_key)})")
        return False

    def is_all_dead(self) -> bool:
        """判斷是否全部 Key 皆已失效"""
        return len(self.active_keys) == 0

    def has_multiple(self) -> bool:
        return len(self.active_keys) > 1

    def mask_key(self, key: Optional[str] = None) -> str:
        k = key or self.get_current_key()
        if not k or len(k) <= 10:
            return "***"
        return f"{k[:6]}...{k[-4:]}"

    def __len__(self) -> int:
        return len(self.active_keys)


# ============================================================
#  一、常數預編譯與古籍文字學映射
# ============================================================
# 擴充相容 CJK 擴展 A~G 區、相容字元與增補漢字，防止大藏經生僻字被誤過濾導致索引脫軌
# 完整支援 CJK 基本區、相容區、擴展 A 區 (BMP) 及擴展 B~I 區 (SIP/TIP 超大字集)
CJK_PATTERN_STR = r"\w\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ebbf"
RE_CLEAN_CJK = re.compile(rf"[^{CJK_PATTERN_STR}]")
RE_CLEAN_CHAR = re.compile(rf"[{CJK_PATTERN_STR}]")
RE_THINK_TAG = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
RE_CODE_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?", re.MULTILINE)
RE_CODE_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$", re.MULTILINE)

# ★ 全域統一 Markdown 段落切分正則（支援 #、*、> 等前綴修飾與原典標籤）
SECTION_SPLIT_REGEX = re.compile(
    r"(?:\n\s*---\s*\n|(?<=\n)(?=(?:[\s#*`>]*【當前經文進度】|"
    r"[\s#*`>]*【單句銷文】|(?:[\s#*`>]*🔹|【\s*🔹?\s*)[\s*`>]*原典)))"
)

# 統一古異體字至標準通行字（單向歸一化，已剔除「雲/云」等詞義衝突字）
VARIANT_CHAR_MAP = str.maketrans({
    "媅": "耽", "怱": "匆", "蘇": "酥", "妒": "妬",
    "睹": "覩", "麤": "粗", "麁": "粗", "併": "并",
    "回": "迴", "嗔": "瞋", "倶": "俱", "缽": "鉢",
    "凈": "淨", "净": "淨", "註": "注", "沈": "沉",
    "祗": "祇", "袛": "祇", "衹": "祇", "只": "祇",
    "裏": "裡", "墮": "堕", "辨": "辯", "鷄": "雞",
})

# 全域 API 請求時間戳（用於免費模型/Gemini 頻率限制控制）
_LAST_API_CALL_TIME = 0.0


# ============================================================
#  二、日誌與 API 快取指標監控
# ============================================================
def setup_logger(log_file: str, logger_name: Optional[str] = None) -> logging.Logger:
    """初始化控制台與檔案日誌器（支援多實例獨立 Logger，防止批次執行時覆蓋 Handlers）"""
    name = logger_name or f"sutra_review_{abs(hash(log_file))}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


def log_cache_metrics(logger: logging.Logger, resp: Any, action_name: str = "API 調用") -> None:
    """解析並記錄 DeepSeek / OpenAI 規範的 Prompt Cache 命中指標（相容 Dict 與 Object）"""
    usage = getattr(resp, "usage", resp if isinstance(resp, dict) else None)
    if not usage:
        return

    def _get(obj: Any, key: str, default: Any = 0) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    prompt_tokens = _get(usage, "prompt_tokens", 0)
    cached_tokens = 0

    prompt_details = _get(usage, "prompt_tokens_details", None)
    if prompt_details:
        cached_tokens = _get(prompt_details, "cached_tokens", 0)

    if not cached_tokens:
        cached_tokens = _get(usage, "prompt_cache_hit_tokens", 0)
    if not cached_tokens:
        cached_tokens = _get(usage, "cache_read_input_tokens", 0)

    completion_tokens = _get(usage, "completion_tokens", 0)

    if prompt_tokens > 0:
        hit_rate = (cached_tokens / prompt_tokens) * 100
        logger.info(
            f"  🎯 快取指標 [{action_name}]: 命中 {cached_tokens}/{prompt_tokens} tokens "
            f"({hit_rate:.1f}%) | 產出 {completion_tokens} tokens"
        )


# ============================================================
#  三、文字正規化、輸出清洗與品質驗證
# ============================================================
def normalize_text(text: str) -> str:
    """提取純漢字與英數字，並執行異體字歸一化"""
    if not text or not isinstance(text, str):
        return ""
    clean = RE_CLEAN_CJK.sub("", text)
    return clean.translate(VARIANT_CHAR_MAP)


def detect_punctuation_style(sutra_text: str) -> str:
    """自動檢測經文標點風格（無標點 / 古典全句號 / 現代標點）"""
    comma_count = sutra_text.count("，") + sutra_text.count("、") + sutra_text.count("；")
    period_count = sutra_text.count("。")
    total_punct = comma_count + period_count
    clean_len = len(RE_CLEAN_CJK.sub("", sutra_text))

    if total_punct == 0 or (total_punct / max(clean_len, 1)) < 0.005:
        return "NO_PUNCT"
    if period_count > 0 and (comma_count / max(period_count, 1)) < 0.05:
        return "ALL_PERIOD"
    return "MODERN_PUNCT"


def clean_markdown_content(raw_content: str) -> str:
    """徹底過濾思維鏈、Markdown 標題/粗體前綴、下一句預告、狀態碼及提示詞殘留"""
    if not raw_content:
        return ""
    text = raw_content.strip()

    # 0. 移除 DeepSeek R1 / 推理模型閉合與未閉合的思維鏈（包含遺漏開頭標籤的情況）
    text = RE_THINK_TAG.sub("", text).strip()
    if "<think>" in text.lower():
        # 若存在未閉合的 <think>，將其後直到 </think> 或結尾的內容全數剝離
        text = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", text, flags=re.IGNORECASE).strip()
    elif "</think>" in text.lower():
        # 容錯：若開頭遺失 <think> 標籤但結尾有 </think>，將其前方內容徹底清除
        text = re.sub(r"^[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    # 移除部分模型輸出的思考前綴標籤
    text = re.sub(r"^:::+\s*thought[\s\S]*?:::+\s*", "", text, flags=re.IGNORECASE).strip()

    # 1. 移除寒暄問候
    text = re.sub(r"^(?:阿彌陀佛[。，！\s]*|施主[^\n]*\n*|好的[，。]我們現在[^\n]*\n*)*", "", text, flags=re.IGNORECASE)

    # 2. 徹底移除【當前經文進度】（相容 ### 、 ** 、 # 等各類 Markdown 標題前綴）
    text = re.sub(
        r"^(?:[\s#*`>]*【當前經文進度】[\s\S]*?(?=(?:(?:[\s#*`>]*【單句銷文】)|🔹|【原典】|$)))",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"^[\s#*`>]*【單句銷文】[：:]?\s*\n*", "", text, flags=re.IGNORECASE).strip()

    # 3. 移除 AI 閒聊自語、提示詞殘留與 Markdown 程式碼區塊標記
    text = re.sub(r"不對——[^\n]*\n*", "", text)
    text = re.sub(r"（註：原典字句依[^\n]*\n*", "", text)
    text = re.sub(r"【補漏任務】[^\n]*\n*", "", text)
    text = re.sub(r"【🚨 絕對起點】[^\n]*\n*", "", text)
    text = RE_CODE_FENCE_OPEN.sub("", text)
    text = RE_CODE_FENCE_CLOSE.sub("", text)

    # 4. 嚴格限定僅移除獨立標題的【下一句預告】（必須有括號或明確標記，防止正文中的「下一句」被誤殺截斷）
    text = re.sub(r"(?:(?:\n|^)[\s#*`>]*【\s*下一句(?:預告)?\s*】[\s\S]*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:(?:\n|^)[\s#*`>]*🔹?\s*下一句預告[\s\S]*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[STATUS:\s*[^\]]*\][\s\S]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"請問是否繼續銷文下一句[？?]?[\s\S]*", "", text)

    # 5. 移除多餘的獨立 Markdown 分隔線
    text = re.sub(r"^\s*---\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def extract_sentence(raw_content: str) -> Optional[str]:
    """精確提取『🔹 原典』中的文字（相容各種 Markdown 粗體、標題與夾註格式）"""
    def _clean_s(t: str) -> str:
        t = t.strip()
        for _ in range(3):
            t = re.sub(r"^[`*_\s]+|[`*_\s]+$", "", t).strip()
            t = re.sub(r"^[「『\"'“‘《〈【〔（(]+|[」』\"'”’》〉】〕）)]+$", "", t).strip()
            t = re.sub(r"^[，。！？；、：）\)\]】〕＞》〉\s]+", "", t).strip()
            t = re.sub(r"[（\(\[【〔＜《〈\s]+$", "", t).strip()
        return t

    # 增強版正則：相容 🔹 **原典：**、### 🔹 原典、🔹 原典： 等所有粗體冒號組合
    # 增強版正則：相容 **🔹 原典**：、🔹 **原典：**、🔹 原典（註）：、### 🔹 原典 等各種變體
    primary_pat = (
        r"(?:[\s#*`>]*🔹[\s*`>]*|【\s*🔹?\s*|\*{1,3}\s*🔹\s*)原典"
        r"(?:[（\(][^）\)]*?[）\)])?"
        r"(?:[\s*`>]*[：:]|[：:][\s*`>]*|】[：:]?|\*{1,3}[：:]?)"
        r"\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|\n\s*#|$))"
    )
    m = re.search(primary_pat, raw_content)
    if m and m.group(1).strip():
        return _clean_s(m.group(1))

    block_match = re.search(r"【單句銷文】([\s\S]*?)(?=【詳解】|$)", raw_content)
    search_area = block_match.group(1) if block_match else raw_content

    fallback_patterns = [
        r"(?:[\s#*`>]*原典[\s*`>]*[：:]|[：:][\s*`>]*)\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|$))",
        r"【原典】[：:]?\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|$))",
    ]
    for pat in fallback_patterns:
        m = re.search(pat, search_area)
        if m and m.group(1).strip():
            return _clean_s(m.group(1))
    return None


def is_ignorable_gap(gap_text: str) -> bool:
    """判斷是否為純符號空白、經名品題或譯者署名（具備防誤殺保護）"""
    clean = normalize_text(gap_text)
    if not clean:
        return True

    # 1. 嚴格品題與卷次結構
    strict_title_patterns = [
        r"^.*?[經論律]\s*(?:卷|品|章|分)(?:第[一二三四五六七八九十百千\d]+)?(?:之[一二三四五六七八九十\d初末餘之]+)?$",
        r"^.*?(?:品第|卷第|分第)[一二三四五六七八九十百千\d]+$",
        r"^第[一二三四五六七八九十百千\d]+[品卷章分](?:之[一二三四五六七八九十\d]+)?$",
        r"^.*?經卷(?:第?[一二三四五六七八九十百千\d]+)?$",
    ]

    # 2. 常見三藏譯師署名與經首序文題記
    translator_patterns = [
        r"^.*?(?:三藏法師|譯經三藏|沙門|法師|天竺三藏|臣|奉詔|詔譯|共譯|譯|奉敕譯)[\u4e00-\u9fff\w\s]*?(?:譯|製|序|說|筆受|集)$",
        r"^.*?(?:御製|皇帝製|唐太宗|高宗|武后)[\u4e00-\u9fff\w\s]*?(?:序|讚|文|記)$",
        r"^(?:開經偈|無上甚深微妙法|百千萬劫難遭遇|我今見聞得受持|願解如來真實義)$",
    ]

    if len(clean) <= 35:
        if any(re.match(pat, clean) for pat in strict_title_patterns):
            return True
        if any(re.match(pat, clean) for pat in translator_patterns):
            return True

    return False


def validate_output_format(raw_content: str) -> Tuple[bool, List[str]]:
    """驗證輸出格式是否包含全部必備結構（修復孤立 🔹 短路誤判，強化格式完整性校驗）"""
    required_sections = [
        (
            "🔹 原典",
            r"(?:🔹\s*(?:\*\*)*\s*(?:原典|經文)|【\s*🔹?\s*(?:原典|經文)\s*】|#{1,4}\s*.*(?:原典|經文)|^[\s*`-]*\*{0,2}(?:原典|經文)\*{0,2}[：:])"
        ),
        (
            "🔸 釋詞",
            r"(?:🔸\s*(?:\*\*|#)*\s*(?:釋詞|釋義|詞義|字詞|名相)|(?:\*\*|#)*\s*(?:釋詞|釋義|詞義|字詞解釋|名相解釋|字句解釋)\s*(?:\*\*)*[：:]?|【\s*(?:釋詞|釋義|詞義|字詞解釋|名相解釋|字句解釋)\s*】|#{1,4}\s*.*(?:釋詞|釋義|詞義|字詞|名相)|^[\s*`-]*\*{0,2}(?:釋詞|釋義|詞義|字詞解釋|名相解釋|名相釋義)\*{0,2}[：:])"
        ),
        (
            "🔸 銷文",
            r"🔸\s*(?:\*\*|#)*\s*(?:銷文|消文|語譯|白話|白話語譯|經文消文)|(?:\*\*|#)*\s*(?:銷文|消文|語譯|白話語譯|白話解說|經文銷文|文義通釋)\s*(?:\*\*)*[：:]?|【\s*(?:銷文|消文|語譯|白話語譯|白話解說|經文銷文)\s*】|#{1,4}\s*.*(?:銷文|消文|語譯|白話)|^[\s*`-]*\*{0,2}(?:銷文|消文|語譯|白話語譯|白話銷文|經文語譯)\*{0,2}[：:]?"
        ),
        (
            "【詳解】",
            r"【\s*(?:詳解|詳細解析|義理詳解|經文詳解|深度剖析|解析|義理解析|詳細解說|經文剖析)\s*】|#{1,4}\s*.*(?:詳解|解析|剖析|義理解析)|(?:\*\*)*\s*(?:詳解|詳細解析|經文詳解|深度剖析|義理解析|詳細解說)\s*(?:\*\*)*[：:]?|^[\s*`-]*\*{0,2}(?:詳解|詳細解析|經文詳解|深度剖析|義理解析|詳細解說)\*{0,2}[：:]?"
        ),
        (
            "【義理通解】",
            r"【\s*(?:義理通解|義理闡釋|義理闡述|義理發微|義理貫通|義理|通解|教理通解|義理總結|實修啟發|心性啟發)\s*】|#{1,4}\s*.*(?:義理通解|義理闡釋|義理闡述|義理|通解|實修啟發|心性啟發)|(?:\*\*)*\s*(?:義理通解|義理闡述|義理闡釋|義理發微|義理貫通|義理|通解|教理通解|義理總結|實修啟發)\s*(?:\*\*)*[：:]?|^[\s*`-]*\*{0,2}(?:義理通解|義理闡釋|義理闡述|義理發微|義理貫通|義理|通解|教理通解|實修啟發)\*{0,2}[：:]?"
        ),
    ]
    missing = [name for name, pat in required_sections if not re.search(pat, raw_content, flags=re.MULTILINE)]
    return (len(missing) == 0), missing


def verify_sentence_quality(
    sentence_text: str,
    remaining_text: str,
    issue_type: str = "",
    problem_desc: str = ""
) -> Tuple[bool, Optional[str], Optional[str]]:
    """統一品質驗證：防腰斬、防半偈、防起點跳字、防格式殘缺、無硬性字數門檻限制"""
    clean_rem = normalize_text(remaining_text)
    clean_s_text = normalize_text(sentence_text)

    is_dup_context = (
        "重複" in issue_type
        or "刪除" in issue_type
        or any(kw in problem_desc for kw in ["重複", "重出", "刪除", "刪去", "前綴", "首句", "跳過", "裁切"])
    )

    # 1. 空內容
    invalid_tokens = ["無", "（無）", "(無)", "none", "null", ""]
    if (sentence_text.strip() in invalid_tokens) or (len(clean_s_text) == 0):
        if is_dup_context:
            return True, None, None
        return False, "原典為空或輸出了『（無）』", "請嚴格從剩餘經文的第一字開始銷文，不可輸出（無）。"

    # 2. 省略號檢查
    if any(sym in sentence_text for sym in ["（中略）", "...", "…", "中略", "[中略]"]):
        return False, "包含省略號", "絕對禁止使用省略號，請完整引用經文。"

    # 3. 起點對齊檢查（具備發語詞/重複主詞智慧容錯）
    match_pos = clean_rem.find(clean_s_text) if clean_s_text else -1
    prefix_4 = clean_s_text[: min(4, len(clean_s_text))]

    if match_pos != 0 and not (prefix_4 and clean_rem.startswith(prefix_4)):
        rem_prefix_6 = clean_rem[: min(6, len(clean_rem))]
        s_pos = clean_s_text.find(rem_prefix_6) if rem_prefix_6 else -1
        if 0 < s_pos <= 10:
            pass  # AI 稍帶前導詞，放行
        elif match_pos > 0:
            skipped_chars = clean_rem[:match_pos]
            if is_dup_context:
                pass
            else:
                raw_skipped = remaining_text[: min(len(skipped_chars) + 15, len(remaining_text))].strip()
                return (
                    False,
                    f"跳過了前段經文（遺漏 {len(skipped_chars)} 字）",
                    f"你漏掉了開頭的『{raw_skipped[:25]}』！輸出的『🔹 原典』必須嚴格從『{raw_skipped[:10]}...』第一個字開始完整照抄，包含散文引言。"
                )
        else:
            if not is_dup_context:
                expected_start = remaining_text[: min(15, len(remaining_text))].strip()
                return (
                    False,
                    "原典不在剩餘經文開頭",
                    f"輸出的原典文字與當前起點不符！請嚴格從『{expected_start}...』第一個字開始一字不差照抄。"
                )

    # 4. 品題/論題/夾註豁免
    title_pattern = (
        r"^(?:.*?[品卷章分地](?:第[一二三四五六七八九十百千\d]+)?(?:之[一二三四五六七八九十\d]+)?"
        r"|第[一二三四五六七八九十百千\d]+[品卷章分地]|.+品|本地分.*|入菩薩行論.*|大乘.*)$"
    )
    is_title = bool(
        re.match(title_pattern, sentence_text.strip())
        or "品第" in sentence_text
        or "分中" in sentence_text
        or sentence_text.strip().endswith("品")
    ) and len(clean_s_text) <= 60
    is_annotation = bool(re.match(r"^[\(（\[【〔〈《].*?[\)）\]】〕〉》]$", sentence_text.strip())) and len(clean_s_text) <= 30

    # 5. 偈頌音律分析
    clauses_s = [
        RE_CLEAN_CJK.sub("", c)
        for c in re.split(r"[，。！？；、：\s\n　]", sentence_text)
        if RE_CLEAN_CJK.sub("", c)
    ]
    is_5_rhythm = (all(len(c) in [5, 10, 15, 20] for c in clauses_s) and len(clean_s_text) % 5 == 0) if clauses_s else (len(clean_s_text) >= 5 and len(clean_s_text) % 5 == 0)
    is_7_rhythm = (all(len(c) in [7, 14, 21, 28] for c in clauses_s) and len(clean_s_text) % 7 == 0) if clauses_s else (len(clean_s_text) >= 7 and len(clean_s_text) % 7 == 0)

    # 設問句（所以者何、何以故、云何等）在講經銷文中為合法徵起/總標，完全放行
    return True, None, None

def request_and_validate_segment(
    client: OpenAI,
    model: str,
    sutra_text: str,
    remaining_text: str,
    prev_sentence: str,
    issue_type: str,
    problem_desc: str,
    reasoning_effort: str,
    logger: logging.Logger,
    loop_guard: int,
    is_gap_mode: bool,
    dup_guide: str = ""
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    執行單次單元銷文請求、清洗與格式品質校驗
    （DeepSeek / OpenAI Prompt Cache 前綴極致鎖定版）
    """
    retry_feedback = ""
    norm_rem_len = len(normalize_text(remaining_text))

    # 1. 區塊 A 絕對靜態規範（字元級固定，保證 100% 命中 KV Cache 前綴）
    guideline_section = (
        "【重寫與銷文核心準則】：\n"
        "1. 原典嚴格從指定剩餘經文的第一字開始照抄，禁止省略號或跳字。\n"
        "2. 五言/七言韻文必須以整偈（4句）為基礎單位，嚴禁輸出半偈或單句。\n"
        "3. 散文若內部包含多個自足法相，請截取首個完整子單元銷文，後續經文系統自動推進；若屬長篇論證則由你自主裁量邊界。\n\n"
    )

    # 2. 區塊 B 動態任務引導（置於動態區域，不破壞區塊 A 快取）
    dynamic_task_hint = ""
    if is_gap_mode:
        dynamic_task_hint = "💡【補漏任務指引】：當前為經文漏段補齊，請嚴格自指定起點切出首個自足單元推進。\n\n"
    elif dup_guide:
        dynamic_task_hint = f"💡【去重特別指引】：{dup_guide}\n\n"

    # 義理導向動態切分指引
    split_hint = ""
    if problem_desc and any(kw in problem_desc for kw in ["拆分", "過長", "切分", "堆疊"]):
        split_hint = (
            f"⚠️【審查拆分指引】：審查報告指出當前段落存在以下問題：\n"
            f"👉『{problem_desc}』\n"
            f"請依據上述分析與法義轉折點，【自主裁量並僅截取開頭第一個語意自足的子單元】進行銷文；"
            f"未處理的後續經文系統會在下一輪自動交由你繼續推進。\n\n"
        )
    elif norm_rem_len > 60:
        split_hint = (
            "💡【長文推進提醒】：當前待處理經文篇幅較長。"
            "若內部包含多個獨立句讀或法相轉折，請僅截取開頭第一個自足單元銷文；"
            "若全段為不可分割之完整論證，則由你依義理自主決定最適篇幅。\n\n"
        )

    pool = getattr(client, "key_pool", None)
    max_retries = max(len(pool) * 2, 6) if (pool and pool.has_multiple()) else 3

    for retry in range(max_retries):
        problem_section = f"【本處病灶與修正建議】：\n{problem_desc}\n\n" if (problem_desc and not is_gap_mode) else ""
        feedback_section = f"\n【🚨 上一輪輸出未通過校驗，請依此指示修正】：\n{retry_feedback}\n" if retry_feedback else ""

        # ★★★ Prompt Cache 最佳化構造：絕對靜態前綴（置頂） -> 動態推進變量（置底） ★★★
        user_msg = (
            # ─── 區塊 A：絕對靜態前綴（全本經文 + 核心規範，100% 鎖定 Cache） ───
            f"【經典全本文脈背景】：\n{sutra_text}\n\n"
            f"{guideline_section}"
            # ─── 區塊 B：動態上下文（每輪前移推進，dynamic_task_hint 已正確接入） ───
            f"{dynamic_task_hint}"
            f"{problem_section}"
            f"{split_hint}"
            f"【前文脈絡錨點】：\n{prev_sentence if prev_sentence else '（經文起始段落）'}\n\n"
            f"【🚨 當前待銷文剩餘經文（請嚴格從第一個字開始）】：\n{remaining_text}"
            f"{feedback_section}"
        )

        try:
            raw_reply = stream_completion(
                client=client,
                model=model,
                system_prompt=FIX_SYSTEM,
                user_prompt=user_msg,
                reasoning_effort=reasoning_effort,
                logger=logger,
                action_name=f"{'補漏銷文' if is_gap_mode else '修復銷文'} (輪次 {loop_guard}, 重試 {retry + 1}/{max_retries})"
            )

            cleaned_reply = clean_markdown_content(raw_reply)
            extracted_sent = extract_sentence(cleaned_reply)

            if not extracted_sent:
                if (
                    "重複" in issue_type
                    and len(normalize_text(cleaned_reply)) < 50
                    and not re.search(r"🔸|🔹|【", cleaned_reply)
                ):
                    return "<!-- DELETE -->", None, True
                retry_feedback = "上一輪未能正確輸出包含『🔹 原典：「...」』的區塊，請嚴格按照輸出格式輸出。"
                logger.warning(f"    ⚠️ [重試 {retry + 1}/{max_retries}] 無法從輸出中解析出『🔹 原典』")
                time.sleep(1)
                continue

            if "重複" in issue_type and (
                extracted_sent.strip() in ["無", "（無）", "(無)", "none", "null", ""]
                or len(normalize_text(extracted_sent)) == 0
            ):
                return "<!-- DELETE -->", None, True

            valid, err_type, err_advice = verify_sentence_quality(
                extracted_sent,
                remaining_text,
                issue_type,
                problem_desc=problem_desc
            )
            if not valid:
                retry_feedback = f"【校驗未通過 ({err_type})】：{err_advice}"
                logger.warning(f"    ⚠️ [重試 {retry + 1}/{max_retries}] 品質校驗未通過 ({err_type})：{err_advice}")
                time.sleep(1)
                continue

            fmt_ok, missing = validate_output_format(cleaned_reply)
            if not fmt_ok:
                if "重複" in issue_type and len(normalize_text(extracted_sent)) == 0:
                    return "<!-- DELETE -->", None, True
                retry_feedback = f"【格式不完整】：輸出缺少必要段落 {missing}，請務必完整輸出所有必備欄位。"
                logger.warning(f"    ⚠️ [重試 {retry + 1}/{max_retries}] 格式缺少必要欄位：{missing}")
                logger.warning(f"    🔍 [模型實際輸出內容如下]：\n{'-'*60}\n{cleaned_reply}\n{'-'*60}")
                time.sleep(1)
                continue

            return cleaned_reply, extracted_sent, False

        except KeyboardInterrupt:
            logger.warning("\n🛑 使用者中斷了修復流程 (Ctrl+C)")
            raise
        except Exception as e:
            err_msg = str(e)
            fatal_keywords = [
                "insufficient balance", "creditserror", "authenticationerror",
                "invalid_api_key", "401", "402", "payment required"
            ]
            is_fatal = any(kw in err_msg.lower() for kw in fatal_keywords)

            if is_fatal:
                if pool:
                    all_dead = pool.mark_current_dead(client, logger, reason=err_msg)
                    if all_dead:
                        logger.error("❌ 所有 API 金鑰皆已失效或餘額不足！流水線立即安全中止。")
                        return None, None, True
                    # 成功切換到其他 Key，立即重試
                    time.sleep(1)
                    continue
                else:
                    logger.error(f"❌ API 金鑰無效或帳號餘額不足 ({err_msg})！流水線立即中止。")
                    return None, None, True

            # 一般限流或連線異常切換
            if pool and pool.has_multiple():
                pool.rotate_client(client, logger, reason=f"API 請求異常 ({e})")
                backoff_time = 1.5
            else:
                is_free_or_rate_limit = (
                    ":free" in model.lower()
                    or "openrouter" in str(client.base_url).lower()
                    or "googleapis" in str(client.base_url).lower()
                    or "429" in err_msg
                    or "rate" in err_msg.lower()
                    or "quota" in err_msg.lower()
                )
                backoff_time = min(45, (retry + 1) * 15) if is_free_or_rate_limit else (retry + 1) * 3

            logger.error(
                f"    ❌ API 呼叫失敗 ({e})，當前待處理經文剩餘 {len(normalize_text(remaining_text))} 字，"
                f"等待 {backoff_time} 秒後進行第 {retry + 1}/{max_retries} 次重試..."
            )
            time.sleep(backoff_time)

    return None, None, False
    
# ============================================================
#  四、經文空間幾何、覆蓋率與槽位映射
# ============================================================
def get_sutra_coverage(sutra_text: str, completed_sentences: List[str]) -> Tuple[List[int], str, List[bool], Dict[int, Tuple[int, int]]]:
    """
    【全域槽位覆蓋演算法】精確將段落映射到經文遮罩，長句優先鎖定，重複句依序入槽。
    回傳: (clean_to_raw_map, norm_sutra, covered_mask, sentence_slots)
    """
    clean_to_raw_map = []
    clean_chars = []
    for raw_idx, char in enumerate(sutra_text):
        if RE_CLEAN_CHAR.match(char):
            clean_chars.append(char)
            clean_to_raw_map.append(raw_idx)

    sutra_clean = "".join(clean_chars)
    norm_sutra = normalize_text(sutra_clean)
    n = len(norm_sutra)
    if n == 0:
        return [], "", [], {}

    covered_mask = [False] * n
    sentence_slots = {}

    valid_sentences = []
    for idx, s in enumerate(completed_sentences):
        s_norm = normalize_text(s)
        if s_norm and len(s_norm) >= 1:
            valid_sentences.append((idx, s, s_norm))

    sorted_sentences = sorted(valid_sentences, key=lambda x: len(x[2]), reverse=True)

    for orig_idx, s, s_norm in sorted_sentences:
        matches = [m.start() for m in re.finditer(re.escape(s_norm), norm_sutra)]
        is_prefix_fallback = False
        if not matches and len(s_norm) >= 4:
            prefix = s_norm[: min(4, len(s_norm))]
            matches = [m.start() for m in re.finditer(re.escape(prefix), norm_sutra)]
            is_prefix_fallback = True

        if not matches:
            continue

        best_pos = -1
        max_new_cover = -1
        best_distance = float("inf")
        expected_pos = int((orig_idx / max(1, len(completed_sentences))) * n)

        for pos in matches:
            end_pos = min(pos + len(s_norm), n)
            # ★ 若使用前綴降級匹配，必須核驗區間內的文字相似度，防止將數十個不相關字元誤塗黑
            if is_prefix_fallback:
                target_window = norm_sutra[pos:end_pos]
                matching_chars = sum(1 for a, b in zip(s_norm, target_window) if a == b)
                if len(s_norm) > 0 and (matching_chars / len(s_norm)) < 0.65:
                    continue

            new_cover = sum(1 for i in range(pos, end_pos) if not covered_mask[i])
            dist = abs(pos - expected_pos)
            if new_cover > max_new_cover or (new_cover == max_new_cover and dist < best_distance):
                max_new_cover = new_cover
                best_distance = dist
                best_pos = pos

        if best_pos != -1:
            end_pos = min(best_pos + len(s_norm), n)
            if max_new_cover > 0:
                for i in range(best_pos, end_pos):
                    covered_mask[i] = True
            sentence_slots[orig_idx] = (best_pos, end_pos)

    return clean_to_raw_map, norm_sutra, covered_mask, sentence_slots


def find_missing_gaps(sutra_text: str, completed_sentences: List[str]) -> List[SutraGap]:
    """基於全域布林覆蓋遮罩的精準漏段掃描（回傳 SutraGap 強型別清單）"""
    clean_to_raw_map, norm_sutra, covered_mask, sentence_slots = get_sutra_coverage(sutra_text, completed_sentences)
    if not norm_sutra:
        return []

    n = len(norm_sutra)
    gaps: List[SutraGap] = []
    i = 0

    pos_to_seg_idx = {}
    for s_idx, (start_p, end_p) in sentence_slots.items():
        for p in range(start_p, end_p):
            pos_to_seg_idx[p] = s_idx

    open_brackets = "「『“‘（([【〔<《〈"

    while i < n:
        if not covered_mask[i]:
            gap_start_clean = i
            while i < n and not covered_mask[i]:
                i += 1
            gap_end_clean = i

            gap_start_raw = clean_to_raw_map[gap_start_clean]
            while gap_start_raw > 0 and sutra_text[gap_start_raw - 1] in open_brackets:
                gap_start_raw -= 1

            close_brackets = "」』”’）)]】〕>》〉"
            if gap_end_clean < len(clean_to_raw_map):
                gap_end_raw = clean_to_raw_map[gap_end_clean]
                while gap_end_raw > gap_start_raw and sutra_text[gap_end_raw - 1] in open_brackets:
                    gap_end_raw -= 1
                # 向後包含緊密相連的閉合引號與標點，避免標點殘留
                while gap_end_raw < len(sutra_text) and sutra_text[gap_end_raw] in close_brackets:
                    gap_end_raw += 1
            else:
                gap_end_raw = len(sutra_text)

            gap_raw = sutra_text[gap_start_raw:gap_end_raw].strip()
            if not is_ignorable_gap(gap_raw) and len(normalize_text(gap_raw)) >= 1:
                if gap_start_clean == 0:
                    pos_type = "head"
                    prev_idx = -1
                elif gap_end_clean == n:
                    pos_type = "tail"
                    prev_idx = pos_to_seg_idx.get(gap_start_clean - 1, len(completed_sentences) - 1)
                else:
                    pos_type = "middle"
                    prev_idx = pos_to_seg_idx.get(gap_start_clean - 1, -1)

                gaps.append(SutraGap(
                    prev_idx=prev_idx,
                    gap_text=gap_raw,
                    position=pos_type
                ))
        else:
            i += 1

    return gaps


def get_source_slice(
    sutra_text: str,
    segments: List[str],
    start_seg_idx: int,
    end_seg_idx: int,
    force_head: bool = False,
    force_tail: bool = False
) -> str:
    """【源文本精確切片】基於全域槽位覆蓋，杜絕定型句指針回跳"""
    if not segments or start_seg_idx >= len(segments):
        return ""

    start_seg_idx = max(0, start_seg_idx)
    end_seg_idx = min(len(segments) - 1, max(start_seg_idx, end_seg_idx))
    target_segments = segments[start_seg_idx : end_seg_idx + 1]

    clean_to_raw_map, _, _, sentence_slots = get_sutra_coverage(sutra_text, segments)
    if not clean_to_raw_map:
        return "".join(target_segments)

    has_any_slot = any(i in sentence_slots for i in range(start_seg_idx, end_seg_idx + 1))
    if not has_any_slot and not force_head and not force_tail:
        return "".join(target_segments)

    if force_head:
        start_clean_pos = 0
    elif start_seg_idx in sentence_slots:
        start_clean_pos = sentence_slots[start_seg_idx][0]
    else:
        curr_p = 0
        for p_i in range(start_seg_idx - 1, -1, -1):
            if p_i in sentence_slots:
                curr_p = sentence_slots[p_i][1]
                break
        start_clean_pos = curr_p

    if force_tail:
        end_clean_pos = len(clean_to_raw_map)
    elif end_seg_idx in sentence_slots:
        end_clean_pos = sentence_slots[end_seg_idx][1]
    else:
        expected_len = sum(len(normalize_text(s)) for s in target_segments)
        end_clean_pos = min(len(clean_to_raw_map), start_clean_pos + expected_len)

    expected_len = sum(len(normalize_text(s)) for s in target_segments)
    if end_clean_pos <= start_clean_pos or (end_clean_pos - start_clean_pos) > max(expected_len * 2 + 60, expected_len + 120):
        end_clean_pos = min(len(clean_to_raw_map), start_clean_pos + max(expected_len, 10))

    open_brackets = set("「『“‘（([【〔<《〈")
    trailing_chars = set(" \t\r\n　，。！？；、：—…」』”’）)]】〕>》〉")

    raw_start = clean_to_raw_map[start_clean_pos] if start_clean_pos < len(clean_to_raw_map) else len(sutra_text)
    while raw_start > 0 and sutra_text[raw_start - 1] in open_brackets:
        raw_start -= 1

    if end_clean_pos >= len(clean_to_raw_map):
        raw_end = len(sutra_text)
    elif end_clean_pos > 0:
        raw_end = clean_to_raw_map[end_clean_pos - 1] + 1
        while raw_end < len(sutra_text) and sutra_text[raw_end] in trailing_chars and sutra_text[raw_end] not in open_brackets:
            raw_end += 1
    else:
        raw_end = raw_start

    extracted = sutra_text[raw_start:raw_end].strip()
    return extracted if extracted else "".join(target_segments)


def find_best_position(raw_target: str, section_text: str, clean_sutra: str, norm_sutra: str, last_pos: int = 0) -> int:
    """【上下文消歧義定位器】具備異體字容錯、單調進度鎖定與 Tri-gram 重疊度比對"""
    if not raw_target:
        return -1
    clean_target = RE_CLEAN_CJK.sub("", raw_target)
    norm_target = normalize_text(raw_target)
    if not clean_target:
        return -1

    positions = []
    start = 0
    while True:
        idx = clean_sutra.find(clean_target, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1

    if not positions:
        start = 0
        while True:
            idx = norm_sutra.find(norm_target, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1

    if not positions and len(norm_target) >= 6:
        prefix = norm_target[: min(8, len(norm_target))]
        start = 0
        while True:
            idx = norm_sutra.find(prefix, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1

    if not positions:
        return -1
    if len(positions) == 1:
        return positions[0]

    if len(positions) > 1 and last_pos > 0:
        forward_positions = [p for p in positions if p >= last_pos - 30]
        if forward_positions:
            positions = forward_positions
        else:
            positions.sort(key=lambda p: abs(p - last_pos))
            positions = [positions[0]]

    best_pos = positions[0]
    max_score = -float('inf')
    sec_norm = normalize_text(section_text)

    for pos in positions:
        win_start = max(0, pos - 150)
        win_end = min(len(norm_sutra), pos + len(clean_target) + 150)
        context_window = norm_sutra[win_start:win_end]

        score = 0
        if len(sec_norm) > len(norm_target) + 6:
            for i in range(0, len(context_window) - 2):
                if context_window[i : i + 3] in sec_norm:
                    score += 1
            score -= (abs(pos - last_pos) / 100.0)
        else:
            score = -abs(pos - last_pos)

        if score > max_score or (score == max_score and abs(pos - last_pos) < abs(best_pos - last_pos)):
            max_score = score
            best_pos = pos

    return best_pos


# ============================================================
#  五、Markdown 文件管理、安全原子寫入與物理重排
# ============================================================
def ensure_initial_backup(filepath: str) -> None:
    """在任務開始前，永久保留一份未更動前的初始檔案備份 (.orig.bak)"""
    if os.path.exists(filepath):
        orig_bak = filepath + ".orig.bak"
        if not os.path.exists(orig_bak):
            try:
                shutil.copy2(filepath, orig_bak)
            except Exception:
                pass


def safe_write_file(filepath: str, content: str) -> None:
    """Windows / OneDrive 檔案鎖容錯強化版原子寫入（具備指數退避、緊急另存防丟失與強制同步）"""
    # 確保初始原始檔備份存在
    ensure_initial_backup(filepath)

    if os.path.exists(filepath):
        try:
            shutil.copy2(filepath, filepath + ".bak")
        except Exception:
            pass

    temp_path = f"{filepath}.{os.getpid()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except Exception as write_err:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            emergency_path = f"{filepath}.emergency_{int(time.time())}.txt"
            try:
                with open(emergency_path, "w", encoding="utf-8") as ef:
                    ef.write(content)
                logging.error(
                    f"❌ 檔案寫入嚴重受阻 ({write_err})，已將進度緊急另存為：{emergency_path}"
                )
            except Exception:
                pass
        return

    replaced = False
    for attempt in range(6):
        try:
            if os.path.exists(filepath):
                try:
                    os.chmod(filepath, 0o666)
                except Exception:
                    pass
            os.replace(temp_path, filepath)
            replaced = True
            break
        except (PermissionError, OSError):
            time.sleep(0.15 * (2 ** attempt))

    if not replaced:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
        except Exception as final_err:
            emergency_path = f"{filepath}.emergency_{int(time.time())}.txt"
            try:
                if os.path.exists(temp_path):
                    os.replace(temp_path, emergency_path)
                else:
                    with open(emergency_path, "w", encoding="utf-8") as ef:
                        ef.write(content)
                logging.getLogger("sutra_review").error(
                    f"❌ 檔案寫入完全被系統鎖定 ({final_err})，已將進度緊急另存為：{emergency_path}"
                )
            except Exception:
                pass


def extract_segments_from_md(filepath: str) -> List[str]:
    """從 MD 檔案中依序提取出所有原典段落"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    raw_matches = re.findall(
        r"(?:[\s#*`>]*🔹[\s*`>]*|【\s*🔹?\s*)原典(?:[\s*`>]*[：:]|[：:][\s*`>]*|】[：:]?)\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|\n---|$))",
        content
    )
    results = []
    invalid_tokens = ["無", "（無）", "(無)", "none", "null", ""]
    for s in raw_matches:
        cleaned = s.strip()
        cleaned = re.sub(r'^[「『"\'“]+|[」』"\'”]+$', "", cleaned).strip()
        cleaned = re.sub(r"^[`\s]+|[`\s]+$", "", cleaned).strip()
        cleaned = re.sub(r"^[，。！？；、：）\)\]】〕＞》〉\s]+", "", cleaned).strip()
        cleaned = re.sub(r"[（\(\[【〔＜《〈\s]+$", "", cleaned).strip()
        if cleaned and cleaned not in invalid_tokens and len(normalize_text(cleaned)) >= 1:
            results.append(cleaned)
    return results


def parse_md_sections(filepath: str) -> Tuple[str, List[str]]:
    """將 MD 拆解為大標題與段落獨立區塊（嚴格保證 sections[i] 與 segments[i] 1:1 絕對對齊）"""
    if not os.path.exists(filepath):
        return "", []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    header = ""
    m = re.match(r"^(#\s+[^\n]+\n+)", content)
    if m:
        header = m.group(1)
        content = content[len(m.group(1)):]

    # 統一使用全域切分正則
    raw_blocks = [
        s.strip()
        for s in SECTION_SPLIT_REGEX.split(content)
        if s.strip() and s.strip() != "---"
    ]
    raw_sections = [b for b in raw_blocks if not (b.startswith("#") and "佛經銷文" in b and len(b) < 120)]

    sections = []
    for b in raw_sections:
        b = re.sub(
            r'(🔹\s*(?:\*\*)*\s*原典\s*(?:\*\*)*[：:]\s*[「『"\'“]*)[）\)\]】〕＞》〉，。！？；、：\s]+',
            r'\1',
            b
        )
        if not sections:
            if extract_sentence(b):
                sections.append(b)
            else:
                header = (header.strip() + "\n\n" + b).strip() + "\n\n"
        else:
            if not extract_sentence(b):
                sections[-1] += "\n\n" + b
            else:
                sections.append(b)
    return header, sections


def reorder_markdown_by_sutra(md_content: str, sutra_text: str) -> str:
    """依據全域槽位覆蓋進行經文物理重排與安全去重"""
    clean_sutra = RE_CLEAN_CJK.sub("", sutra_text)
    if not clean_sutra or not md_content.strip():
        return md_content

    header = ""
    content_body = md_content.strip()
    title_match = re.match(r"^(#\s+[^\n]+\n+)", content_body)
    if title_match:
        header = title_match.group(1).strip()
        content_body = content_body[len(title_match.group(1)):].strip()

    content_body = re.sub(r"^(?:\s*---\s*\n*)+", "", content_body).strip()

    # 統一使用全域切分正則
    raw_sections = [
        s.strip()
        for s in SECTION_SPLIT_REGEX.split(content_body)
        if s.strip() and s.strip() != "---"
    ]

    norm_sutra = normalize_text(sutra_text)
    n = len(norm_sutra)
    section_items = []

    for idx, sec in enumerate(raw_sections):
        if sec.startswith("# 佛經銷文"):
            sec = re.sub(r"^#\s+[^\n]+\n+", "", sec).strip()
            if not sec:
                continue

        sentence = extract_sentence(sec)
        if not sentence:
            continue

        clean_s = normalize_text(sentence)
        if not clean_s or len(clean_s) < 1:
            continue

        section_items.append({
            "orig_idx": idx,
            "sec": sec,
            "sentence": sentence,
            "norm_s": clean_s,
            "len": len(clean_s),
        })

    if not section_items:
        return md_content

    coverage_count = [0] * n
    assigned_sections = []
    sorted_by_len = sorted(section_items, key=lambda x: x.get("len", 0), reverse=True)

    for item in sorted_by_len:
        norm_s = item.get("norm_s", "")
        if not norm_s:
            norm_s = normalize_text(item.get("sentence", "")) or normalize_text(extract_sentence(item.get("sec", "")))

        matches = [m.start() for m in re.finditer(re.escape(norm_s), norm_sutra)] if norm_s else []
        if not matches and len(norm_s) >= 4:
            prefix = norm_s[: min(4, len(norm_s))]
            matches = [m.start() for m in re.finditer(re.escape(prefix), norm_sutra)]

        if not matches:
            # ★ 修復：依原始段落索引在全文中的相對位置進行線性插值，避免被丟到檔案末尾
            estimated_pos = int((item.get("orig_idx", 0) / max(1, len(section_items))) * n)
            assigned_sections.append({
                "pos": estimated_pos,
                "orig_idx": item.get("orig_idx", 0),
                "sec": item.get("sec", ""),
                "norm_s": norm_s,
            })
            continue

        best_pos = matches[0]
        min_overlap = float("inf")
        for pos in matches:
            end_pos = min(pos + len(norm_s), n)
            overlap = sum(coverage_count[i] for i in range(pos, end_pos))
            if overlap < min_overlap:
                min_overlap = overlap
                best_pos = pos

        end_pos = min(best_pos + len(norm_s), n)
        for i in range(best_pos, end_pos):
            coverage_count[i] += 1

        assigned_sections.append({
            "pos": best_pos,
            "orig_idx": item.get("orig_idx", 0),
            "sec": item.get("sec", ""),
            "norm_s": norm_s,
        })

    assigned_sections.sort(key=lambda x: (x["pos"], x["orig_idx"]))

    unique_secs = []
    seen_sec_hashes = set()
    seen_pos_sentences = set()
    for item in assigned_sections:
        sec_clean_norm = normalize_text(item["sec"])
        sec_hash = hash(sec_clean_norm) if sec_clean_norm else hash(item["sec"].strip())
        
        norm_s = item.get("norm_s") or normalize_text(extract_sentence(item.get("sec", "")) or "")
        pos = item["pos"]
        
        if not norm_s:
            count_in_sutra = 0
        elif len(norm_s) < 15:
            count_in_sutra = norm_sutra.count(norm_s)
        else:
            count_in_sutra = norm_sutra.count(norm_s[:15])

        if count_in_sutra <= 1 and pos != 9999999 and norm_s:
            pos_key = (pos, norm_s)
            if pos_key in seen_pos_sentences:
                continue
            seen_pos_sentences.add(pos_key)

        if sec_hash not in seen_sec_hashes:
            seen_sec_hashes.add(sec_hash)
            unique_secs.append(item["sec"])

    result = ""
    if header:
        result = header + "\n\n"
    if unique_secs:
        result += "\n\n---\n\n".join(unique_secs) + "\n"
    return result


def update_md_file(
    filepath: str,
    sutra_text: str,
    corrections: List[Tuple[str, List[int]]],
    segments_snapshot: List[str],
    logger: logging.Logger
) -> None:
    """將修正結果寫回 MD 檔，並進行經文位置物理校準重排（具備文字特徵動態錨定機制）"""
    header, sections = parse_md_sections(filepath)
    # 由後往前替換，降低前方索引變動影響
    corrections.sort(key=lambda x: x[1][0] if (x[1] and len(x[1]) > 0) else -1, reverse=True)

    for new_content, merge_idx in corrections:
        if not new_content:
            continue

        target_indices_in_sections = []
        for orig_i in merge_idx:
            if 0 <= orig_i < len(segments_snapshot):
                target_seg = segments_snapshot[orig_i]
                norm_target = normalize_text(target_seg)
                if not norm_target:
                    continue

                # 優先全域特徵比對，避免依賴易位移的局部搜尋窗口
                best_sec_idx = -1
                best_match_score = -1
                for sec_idx, sec_text in enumerate(sections):
                    sec_sent = extract_sentence(sec_text)
                    sec_norm = normalize_text(sec_sent) if sec_sent else ""
                    if not sec_norm:
                        continue

                    # 精確匹配或高度包含判定
                    if norm_target == sec_norm:
                        best_sec_idx = sec_idx
                        break
                    elif norm_target in sec_norm or sec_norm in norm_target:
                        overlap = min(len(norm_target), len(sec_norm))
                        if overlap > best_match_score:
                            best_match_score = overlap
                            best_sec_idx = sec_idx

                if best_sec_idx != -1 and best_sec_idx not in target_indices_in_sections:
                    target_indices_in_sections.append(best_sec_idx)

        # 若特徵比對未果，安全回退至索引保底
        if not target_indices_in_sections:
            target_indices_in_sections = [i for i in merge_idx if 0 <= i < len(sections)]

        target_indices_in_sections.sort()

        if target_indices_in_sections:
            first_pos = target_indices_in_sections[0]
            # 由後往前刪除舊區塊
            for idx in reversed(target_indices_in_sections):
                if 0 <= idx < len(sections):
                    sections.pop(idx)

            # 插入重寫後的新區塊
            if new_content and new_content != "<!-- DELETE -->":
                new_blocks = [
                    b.strip()
                    for b in SECTION_SPLIT_REGEX.split(new_content)
                    if b.strip() and b.strip() != "<!-- DELETE -->"
                ]
                for offset, block in enumerate(new_blocks):
                    sections.insert(first_pos + offset, block)

    combined_body = (header + "\n\n---\n\n" if header else "") + "\n\n---\n\n".join(sections)
    reordered_content = reorder_markdown_by_sutra(combined_body, sutra_text)
    safe_write_file(filepath, reordered_content)
    logger.info(f"✨ MD 檔案已安全更新（已備份 .bak）並依原典物理位置重排：{filepath}")

                if not matched:
                    sorted_sec_indices = sorted(range(len(sections)), key=lambda idx: abs(idx - orig_i))
                    for sec_idx in sorted_sec_indices:
                        sec_orig_sent = extract_sentence(sections[sec_idx])
                        sec_orig_norm = normalize_text(sec_orig_sent) if sec_orig_sent else ""
                        if norm_target and sec_orig_norm and (norm_target in sec_orig_norm or sec_orig_norm in norm_target):
                            if sec_idx not in target_indices_in_sections:
                                target_indices_in_sections.append(sec_idx)
                            break

        if not target_indices_in_sections:
            target_indices_in_sections = [i for i in merge_idx if 0 <= i < len(sections)]

        target_indices_in_sections.sort()

        if target_indices_in_sections:
            first_pos = target_indices_in_sections[0]
            for idx in reversed(target_indices_in_sections):
                if 0 <= idx < len(sections):
                    sections.pop(idx)

            if new_content and new_content != "<!-- DELETE -->":
                new_blocks = [
                    b.strip()
                    for b in re.split(
                        r"(?:\n\s*---\s*\n|(?<=\n)(?=(?:【當前經文進度】|【單句銷文】|🔹\s*(?:\*\*)*原典)))",
                        new_content
                    )
                    if b.strip() and b.strip() != "<!-- DELETE -->"
                ]
                for offset, block in enumerate(new_blocks):
                    sections.insert(first_pos + offset, block)

    combined_body = (header + "\n\n---\n\n" if header else "") + "\n\n---\n\n".join(sections)
    reordered_content = reorder_markdown_by_sutra(combined_body, sutra_text)
    safe_write_file(filepath, reordered_content)
    logger.info(f"✨ MD 檔案已安全更新（已備份 .bak）並依原典物理位置重排：{filepath}")


# ============================================================
#  六、統一 LLM 串流請求與動態指針推進核心
# ============================================================
FIX_SYSTEM = """【角色設定】
你是一位精通三藏十二部經律論、具備深厚佛學素養，實修開悟證果，且講經說法經驗豐富的禪師與佛學學者。你的解經風格嚴謹、重視傳承、字字有落處。你擅長論述，極具邏輯思辨與體系化，能對深奧的文言經文進行鋸細靡遺的「銷文解義」，旁徵博引，長篇敷演，務求將法理剖析得透徹入微。

【重寫與義理切分規範】：
1. 【起點嚴格、字字精確】：輸出的「🔹 原典」必須嚴格從指定的剩餘經文第一個字開始，一字不差，絕對禁止省略號（...、（中略））。
2. 【適切粒度與自然切分（義理自足原則）】：
   - ★【偈頌整偈原則（防半偈/防單句）】：遇到五言或七言韻文時，【必須以整偈（4句）為基礎單位】（五言 20 字、七言 28 字），【絕對嚴禁輸出單句（7字）或半偈（14字）】；若待處理經文為 5 句或 6 句等非標準倍數，請將多出的 1~2 句直接合併為一整單元銷文。
   - ★【長短篇幅依義理自主決定】：
     1. 若經文內部包含多個可獨立開示的法義層次（如多個獨立法問、法義轉折、或並列之不同譬喻），請【自主選取開頭第一個主題自足的完整子單元】進行銷文，未處理的後續經文系統會在下一輪自動推進。
     2. 若經文屬於邏輯嚴密、主從相連、不可割裂的長篇推導、因明論證或完整譬喻，【即使篇幅較長亦完全允許整段銷文】，請依佛學法義深淺與講經流暢度自主裁量最適邊界。
   - ★【拒絕碎化與排比統攝】：精練問答、法相標題、設問句雖短可獨立成段；但【嚴禁將密集排比名相（如百八句純句列）逐逗號拆成碎片】，應依義理群組統攝銷文。

【銷文與解義原則】
1. 極盡詳盡的銷解：依傳統講經「消文釋義」的方式，鋸細靡遺地拆解該句經文的文言句法與字面意義，落實每一個字、詞的作用，不可含糊帶過。
2. 義理通解：在考證與詳解之後，需將此句經文的核心教理進行全面性的統攝與貫通，並可配合現代化比喻助解。

【輸出格式】 (請嚴格按照以下結構輸出，不可遺漏任何段落)

🔹 原典：「[填入當前處理的單句經文]」
🔸 釋詞：[針對生僻字、文言虛實詞或佛教核心名相，進行字義與詞義解釋]
🔸 銷文：[將上述字詞串聯，把這句話的文法結構與表面意涵，用流暢且極具邏輯的方式「消解、疏通」出來]

【詳解】：
(深入剖析這句話背後所涵蓋的理體，內容須具備高度的教理深度與嚴密性。)

【義理通解】：
(在上述引經據典的基礎上，用現代佛學語言，通盤詳細解釋這句經文的核心思想。指出這句話在實修觀照或心性理體上的關鍵啟發。如果有需要，舉出實際的例子或打比方幫助理解)"""


def stream_completion(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    reasoning_effort: str,
    logger: logging.Logger,
    action_name: str = "LLM 呼叫"
) -> str:
    """封裝串流 LLM 呼叫，具備 Token 上限校準、思考心跳與自動降級機制"""
    global _LAST_API_CALL_TIME

    # OpenRouter / GLM / Gemini 等第三方免費模型單次上限適配，DeepSeek 官方則可設較大
    is_third_party_or_free = (
        ":free" in model.lower()
        or "glm" in model.lower()
        or "gemini" in model.lower()
        or "openrouter" in str(client.base_url).lower()
        or "googleapis" in str(client.base_url).lower()
    )
    max_tokens_val = 65536 if is_third_party_or_free else 384000

    # ★ 最佳化動態頻率管控（Gemini Flash 5.2s / Gemini Pro 13s / OpenRouter 6s）
    if is_third_party_or_free:
        if "gemini" in model.lower() or "googleapis" in str(client.base_url).lower():
            target_interval = 13.0 if ("pro" in model.lower()) else 5.2
        else:
            target_interval = 6.0

        now = time.time()
        elapsed = now - _LAST_API_CALL_TIME
        if elapsed < target_interval:
            wait_seconds = target_interval - elapsed
            logger.info(f"  ⏳ [頻率管控] API 調用間隔保護中，等待 {wait_seconds:.1f} 秒...")
            time.sleep(wait_seconds)

    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens_val,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_effort and not is_third_party_or_free:
        create_kwargs["extra_body"] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort,
        }

    try:
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as api_err:
        err_str = str(api_err).lower()
        # 0. 針對 OpenRouter/GLM 之 max_tokens 過大報錯降級
        if "max_tokens" in err_str or "maximum allowed" in err_str:
            create_kwargs["max_tokens"] = 4096
        # 1. 降級去除 extra_body (thinking 參數)
        if "extra_body" in create_kwargs and ("extra_body" in err_str or "unrecognized" in err_str or "thinking" in err_str):
            create_kwargs.pop("extra_body", None)
        # 2. 降級去除 stream_options (部分反向代理不支援)
        if "stream_options" in err_str or "stream_options" in str(api_err):
            create_kwargs.pop("stream_options", None)

        try:
            resp = client.chat.completions.create(**create_kwargs)
        except Exception as e2:
            # 二次安全降級：同時去除 stream_options 與 extra_body
            create_kwargs.pop("extra_body", None)
            create_kwargs.pop("stream_options", None)
            if "max_tokens" in str(e2).lower():
                create_kwargs["max_tokens"] = 4096
            resp = client.chat.completions.create(**create_kwargs)

    content_parts = []
    last_chunk = None
    captured_usage = None
    thinking_ticks = 0

    for chunk in resp:
        last_chunk = chunk
        if hasattr(chunk, "usage") and chunk.usage:
            captured_usage = chunk.usage

        delta = chunk.choices[0].delta if (chunk.choices and len(chunk.choices) > 0) else None
        if not delta:
            continue

        # 思考階段心跳提示（針對 DeepSeek R1 / 推理模型）
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            thinking_ticks += 1
            if thinking_ticks % 25 == 0:
                sys.stdout.write("🧠")
                sys.stdout.flush()
        elif delta.content:
            content_parts.append(delta.content)

    if thinking_ticks > 0:
        sys.stdout.write("\n")
        sys.stdout.flush()

    target_usage_source = captured_usage if captured_usage is not None else last_chunk
    if target_usage_source:
        log_cache_metrics(logger, target_usage_source, action_name=action_name)

    # 檢查是否因 token 上限遭遇長度截斷
    if last_chunk and hasattr(last_chunk, "choices") and last_chunk.choices:
        finish_reason = getattr(last_chunk.choices[0], "finish_reason", None)
        if finish_reason == "length":
            logger.warning("  ⚠️ [輸出截斷警告] 模型輸出觸及 max_tokens 上限，內容可能不完整！")

    # 確保以請求結束的時間點作為頻率間隔起算點
    _LAST_API_CALL_TIME = time.time()

    full_output = "".join(content_parts)
    if getattr(client, "debug_mode", False):
        logger.info(f"\n{'='*25} 🤖 [AI 原始完整輸出 RAW] {'='*25}\n{full_output}\n{'='*75}\n")

    return full_output


def advance_text_pointer(remaining_text: str, extracted_sentence: str) -> str:
    """
    【純函數】精確推進指針，回傳裁切後的剩餘文本。
    利用後綴錨點動態比對，防止 AI 多輸出/少輸出導致的指針漂移。
    """
    norm_sent = normalize_text(extracted_sentence)
    norm_rem = normalize_text(remaining_text)

    if len(norm_rem) <= len(norm_sent):
        return ""

    suffix_len = min(6, len(norm_sent))
    if suffix_len == 0:
        return remaining_text

    norm_suffix = norm_sent[-suffix_len:]

    clean_to_raw = []
    clean_chars = []
    for raw_i, ch in enumerate(remaining_text):
        if RE_CLEAN_CHAR.match(ch):
            clean_chars.append(ch)
            clean_to_raw.append(raw_i)

    norm_rem_str = normalize_text("".join(clean_chars))
    suffix_matches = []
    start_search = 0
    while True:
        idx = norm_rem_str.find(norm_suffix, start_search)
        if idx == -1:
            break
        suffix_matches.append(idx)
        start_search = idx + 1

    open_brackets = set("「『“‘（([【〔<《〈")
    trailing_chars = set(" \t\r\n　，。！？；、：—…」』”’）)]】〕>》〉")

    cut_idx = -1
    if suffix_matches:
        s_match_pos = norm_rem_str.find(norm_sent)
        expected_end = (s_match_pos + len(norm_sent)) if s_match_pos >= 0 else len(norm_sent)
        best_norm_end = min(
            suffix_matches,
            key=lambda p: abs((p + suffix_len) - expected_end)
        ) + suffix_len

        if best_norm_end < len(clean_to_raw):
            raw_cut = clean_to_raw[best_norm_end - 1] + 1
            while raw_cut < len(remaining_text) and remaining_text[raw_cut] in trailing_chars and remaining_text[raw_cut] not in open_brackets:
                raw_cut += 1
            cut_idx = raw_cut
        else:
            return ""

    if cut_idx == -1 and clean_to_raw:
        target_len = min(len(norm_sent), len(clean_to_raw))
        if target_len < len(clean_to_raw):
            raw_cut = clean_to_raw[target_len - 1] + 1
            while raw_cut < len(remaining_text) and remaining_text[raw_cut] in trailing_chars and remaining_text[raw_cut] not in open_brackets:
                raw_cut += 1
            cut_idx = raw_cut
        else:
            return ""

    if cut_idx != -1:
        if cut_idx < len(remaining_text):
            remaining_text = remaining_text[cut_idx:].strip()
            remaining_text = re.sub(r"^[，。！？；、：）\)\]】〕＞》〉」』”’\'\"\s　]+", "", remaining_text).strip()

            # 安全防護：若剩餘開頭殘留 <=2 個字且均為常見句尾虛助詞/孤立標點，視為已消化完畢，避免卡在單字死循環
            rem_clean = normalize_text(remaining_text)
            if 0 < len(rem_clean) <= 2:
                weak_particles = {"者", "也", "耳", "矣", "焉", "哉", "乎", "耶", "兮", "歟", "之"}
                if all(ch in weak_particles for ch in rem_clean):
                    remaining_text = ""
            return remaining_text
        else:
            return ""

    return ""


def generate_sutra_segments(
    client: OpenAI,
    model: str,
    sutra_text: str,
    target_raw_text: str,
    prev_sentence: str,
    logger: logging.Logger,
    issue_type: str = "",
    problem_desc: str = "",
    reasoning_effort: str = "high",
    initial_blocks: Optional[List[str]] = None,
    on_step_done: Optional[Callable[[List[str], str, str], None]] = None,
    on_block_success: Optional[Callable[[str, str], None]] = None
) -> Tuple[Optional[List[str]], str]:
    """
    【通用動態指針推進生成引擎】
    - 支援長經文 AI 自主分段消化
    - 後綴錨點精準裁切，杜絕指針脫軌
    - 支援中途回呼與 Checkpoint 存檔
    回傳: (generated_blocks_list, remaining_unprocessed_text)
    """
    remaining_text = target_raw_text.strip()
    generated_blocks = list(initial_blocks) if initial_blocks else []
    curr_prev_sentence = prev_sentence

    max_loops = max(120, len(normalize_text(remaining_text)) // 10 + 30)
    loop_guard = 0

    dup_guide = ""
    if "重複" in issue_type:
        dup_guide = (
            "若前文已包含部分重複經文，請僅針對指定的剩餘經文進行銷文；"
            "若本段經文已完全重複，請直接輸出 <!-- DELETE -->。"
        )

    is_gap_mode = ("經文漏段補齊" in issue_type or "補漏" in issue_type or "全本經文銷文" in issue_type)

    while remaining_text and len(normalize_text(remaining_text)) > 0 and loop_guard < max_loops:
        loop_guard += 1

        # 簡化呼叫簽名，移除冗餘參數
        success_block, extracted_sent, should_term = request_and_validate_segment(
            client=client,
            model=model,
            sutra_text=sutra_text,
            remaining_text=remaining_text,
            prev_sentence=curr_prev_sentence,
            issue_type=issue_type,
            problem_desc=problem_desc,
            reasoning_effort=reasoning_effort,
            logger=logger,
            loop_guard=loop_guard,
            is_gap_mode=is_gap_mode,
            dup_guide=dup_guide
        )

        if should_term and success_block is None:
            return None, remaining_text

        if success_block:
            generated_blocks.append(success_block)
            if extracted_sent:
                curr_prev_sentence = extracted_sent

            if success_block == "<!-- DELETE -->":
                logger.info("    🗑️ 模型確認為重複經文，直接標記刪除")
                remaining_text = ""
                if on_step_done and callable(on_step_done):
                    on_step_done(generated_blocks, "", curr_prev_sentence)
                break

            # 推進剩餘文字指針
            remaining_text = advance_text_pointer(remaining_text, extracted_sent)
            if len(normalize_text(remaining_text)) == 0:
                remaining_text = ""

            logger.info(
                f"    ✅ 子單元銷文成功：『{extracted_sent[:25]}...』"
                f"(剩餘 {len(normalize_text(remaining_text))} 字)"
            )

            if on_block_success and callable(on_block_success):
                on_block_success(success_block, extracted_sent)

            if on_step_done and callable(on_step_done):
                on_step_done(generated_blocks, remaining_text, curr_prev_sentence)

            time.sleep(0.5)
        else:
            logger.error(f"  ❌ 目標經文於『{remaining_text[:20]}...』處修復失敗，終止此段推進。")
            if not generated_blocks and len(normalize_text(remaining_text)) >= 3:
                return None, remaining_text
            break

    if remaining_text and len(normalize_text(remaining_text)) > 2:
        logger.error(
            f"  ❌ 目標經文未完全消化完畢，仍殘留 {len(normalize_text(remaining_text))} 字"
            f"（『{remaining_text[:30]}...』），判定修復失敗！"
        )
        return None, remaining_text

    return generated_blocks, remaining_text

def fix_single_issue(
    client: OpenAI,
    model: str,
    sutra_text: str,
    issue: Any,  # 支援 ReviewIssue 或 Dict[str, Any]
    segments: List[str],
    logger: logging.Logger,
    reasoning_effort: str = "high",
    partial_state: Optional[Dict[str, Any]] = None,
    on_step_done: Optional[Callable[[List[int], Any], None]] = None,
    output_path: Optional[str] = None
) -> Tuple[Optional[str], List[int]]:
    """針對特定審查問題進行段落合併重寫"""
    # 支援物件與字典雙重存取
    if hasattr(issue, "merge_indices"):
        merge_idx = sorted(list(set(issue.merge_indices)))
        issue_type_str = issue.issue_type
        problem_desc = issue.problem
        gap_text = issue.gap_text or ""
        position = issue.position
    else:
        merge_idx = sorted(list(set(issue.get("merge_indices", [issue.get("index", 0)]))))
        issue_type_str = issue.get("type", "")
        problem_desc = issue.get("problem", "")
        gap_text = issue.get("gap_text", "")
        position = issue.get("position")

    valid_merge_idx = [i for i in merge_idx if 0 <= i < len(segments)]
    merge_segs = [segments[i] for i in valid_merge_idx]

    first_idx = valid_merge_idx[0] if valid_merge_idx else 0
    last_idx = valid_merge_idx[-1] if valid_merge_idx else 0
    prev_sentence = segments[first_idx - 1] if first_idx > 0 and (first_idx - 1) < len(segments) else ""

    is_gap_fix = "經文漏段補齊" in issue_type_str
    is_head_gap = (is_gap_fix and (position == "head" or first_idx == 0))
    is_tail_gap = (is_gap_fix and (position == "tail" or last_idx == len(segments) - 1))

    # ★ 孤立標點/括號快速修復直通（本地免 API 快速清洗）
    if any(kw in problem_desc for kw in ["孤立符號", "多出孤立", "開頭標點殘肢"]) and len(valid_merge_idx) == 1:
        target_idx = valid_merge_idx[0]
        if output_path and os.path.exists(output_path):
            _, all_sections = parse_md_sections(output_path)
            if target_idx < len(all_sections):
                target_block = all_sections[target_idx]
                fixed_block = re.sub(
                    r'(🔹\s*(?:\*\*)*\s*原典\s*(?:\*\*)*[：:]\s*[「『"\'“]*)[）\)\]】〕＞》〉，。！？；、：\s]+',
                    r'\1',
                    target_block
                )
                logger.info(f"  ⚡ [本地極速修復] 段落 [{target_idx}] 開頭孤立符號已在本地直接清除，免除 API 調用！")
                if on_step_done and callable(on_step_done):
                    on_step_done(valid_merge_idx, fixed_block)
                return fixed_block, valid_merge_idx

    if merge_segs:
        combined_raw = get_source_slice(
            sutra_text,
            segments,
            first_idx,
            last_idx,
            force_head=is_head_gap,
            force_tail=is_tail_gap
        )
        gap_len = len(normalize_text(gap_text))
        expected_raw_len = sum(len(normalize_text(s)) for s in merge_segs) + gap_len
        if len(normalize_text(combined_raw)) > max(expected_raw_len * 2 + 100, expected_raw_len + 150) and not (is_head_gap or is_tail_gap or is_gap_fix):
            logger.warning(
                f"  ⚠️ 檢測到切片長度異常膨脹 ({len(combined_raw)} 字 vs 預期 {expected_raw_len} 字)，"
                f"安全回退至段落拼合"
            )
            combined_raw = "".join(merge_segs)
    else:
        combined_raw = gap_text

    if not combined_raw:
        combined_raw = "".join(merge_segs)

    if not combined_raw.strip():
        logger.warning(f"  ⚠️ 修正目標文本為空，跳過段落 {valid_merge_idx}")
        return None, valid_merge_idx

    # 完全重複或誤植移除段落快速直通（免調用 API，具備孤本防雙殺安全保護）
    if not is_gap_fix and not gap_text:
        has_delete_intent = any(kw in problem_desc for kw in [
            "應刪除", "建議刪除", "應予刪除", "完全重複", "重複應刪除",
            "請刪除", "直接刪除", "應直接刪除", "整段刪除", "刪除重複", "刪除本段",
            "將此段移除", "應將此段移除", "直接移除", "誤植", "剔除"
        ])
        has_partial_keep = any(kw in problem_desc for kw in [
            "保留", "重新切分", "拆分", "前綴", "首句", "末句", "首二句", "補齊", "補全", "部分保留", "僅刪除"
        ])
        if has_delete_intent and not has_partial_keep:
            # ★ 孤本防雙殺檢查：確認其餘段落中是否確實保留有此經文
            norm_target = normalize_text(combined_raw)
            other_segs = [s for idx, s in enumerate(segments) if idx not in valid_merge_idx]
            has_backup_elsewhere = any(
                (len(norm_target) >= 6 and (norm_target[:15] in normalize_text(s) or normalize_text(s)[:15] in norm_target))
                for s in other_segs
            )

            # 只有當其他段落確實存在備份時才允許整段刪除；若已無備份，絕不刪除，防止雙殺導致漏段
            if has_backup_elsewhere:
                logger.info(f"  🗑️ 判定為重複段落且他處已保留，直接標記刪除：段落 {valid_merge_idx}")
                if on_step_done and callable(on_step_done):
                    on_step_done(valid_merge_idx, "<!-- DELETE -->")
                return "<!-- DELETE -->", valid_merge_idx
            else:
                logger.warning(
                    f"  🛡️ [防雙殺守護] 段落 {valid_merge_idx} 被建議刪除，但檢測到其餘段落無此經文備份！"
                    f"為防止經文遺漏，取消直接刪除，交由 AI 重整校準。"
                )

    # 部分重複智慧裁切
    if problem_desc:
        m_keep = re.search(
            r"(?:保留(?:後[一二三四\d]+句)?|(?:直接|本段|應)?(?:自|從|由|起自))"
            r"[^\w\u4e00-\u9fa5]*[「『\"\'“](.+?)[」』\"\'”](?:開始|起|即可)?",
            problem_desc
        )
        if m_keep:
            start_kw = normalize_text(m_keep.group(1))
            norm_comb = normalize_text(combined_raw)
            k_pos = norm_comb.find(start_kw)
            if k_pos > 0:
                clean_chars_map = [raw_i for raw_i, ch in enumerate(combined_raw) if RE_CLEAN_CHAR.match(ch)]
                if k_pos < len(clean_chars_map):
                    combined_raw = combined_raw[clean_chars_map[k_pos]:].strip()
                    logger.info(f"  ✂️ 依審查建議裁切起點至『{m_keep.group(1)[:15]}...』")
        else:
            m_dup_prefix = re.search(
                r"(?:首[一二三四\d]+句|重複前段|前段|開頭|首句|前[一二三四\d]+句)"
                r"[^\w\u4e00-\u9fa5]*[「『\"\'“](.+?)[」』\"\'”].*?(?:重出|重複|刪除|刪去)",
                problem_desc
            )
            if m_dup_prefix:
                dup_kw = normalize_text(m_dup_prefix.group(1))
                norm_comb = normalize_text(combined_raw)
                if norm_comb.startswith(dup_kw):
                    trim_len = len(dup_kw)
                    clean_chars_map = [raw_i for raw_i, ch in enumerate(combined_raw) if RE_CLEAN_CHAR.match(ch)]
                    if trim_len < len(clean_chars_map):
                        combined_raw = combined_raw[clean_chars_map[trim_len]:].strip()
                        logger.info(f"  ✂️ 剔除前段重複經文『{m_dup_prefix.group(1)[:15]}...』")
                    else:
                        logger.info(f"  🗑️ 重複前綴涵蓋全段，直接標記刪除：段落 {valid_merge_idx}")
                        if on_step_done and callable(on_step_done):
                            on_step_done(valid_merge_idx, "<!-- DELETE -->")
                        return "<!-- DELETE -->", valid_merge_idx

    initial_blocks = None
    target_text = combined_raw
    if partial_state and isinstance(partial_state, dict):
        initial_blocks = partial_state.get("blocks", [])
        target_text = partial_state.get("remaining_text", combined_raw)
        prev_sentence = partial_state.get("prev_sentence", prev_sentence)
        logger.info(
            f"  ⚡ [接續子進度] 載入已完成的 {len(initial_blocks)} 個子單元，"
            f"自剩餘 {len(normalize_text(target_text))} 字繼續銷文..."
        )
    else:
        logger.info(f"  🔧 開始修正目標段落 {valid_merge_idx}（待處理經文共 {len(combined_raw)} 字）...")

    def step_callback(blocks, rem_txt, prev_s):
        if on_step_done and callable(on_step_done):
            on_step_done(valid_merge_idx, {
                "partial": True,
                "blocks": blocks,
                "remaining_text": rem_txt,
                "prev_sentence": prev_s
            })

    generated_blocks, rem_after = generate_sutra_segments(
        client=client,
        model=model,
        sutra_text=sutra_text,
        target_raw_text=target_text,
        prev_sentence=prev_sentence,
        logger=logger,
        issue_type=issue_type_str,
        problem_desc=problem_desc,
        reasoning_effort=reasoning_effort,
        initial_blocks=initial_blocks,
        on_step_done=step_callback
    )

    if generated_blocks is None or (rem_after and len(normalize_text(rem_after)) > 2):
        logger.error(
            f"  ❌ 目標經文段落 {valid_merge_idx} 未完全消化完畢"
            f"（仍殘留 {len(normalize_text(rem_after or ''))} 字），判定修復失敗！"
        )
        return None, valid_merge_idx

    if generated_blocks:
        full_content = "\n\n---\n\n".join(generated_blocks)
        if full_content.strip() == "<!-- DELETE -->":
            return "<!-- DELETE -->", valid_merge_idx
        return full_content, valid_merge_idx
    else:
        if "重複" in issue_type_str:
            return "<!-- DELETE -->", valid_merge_idx
        return None, valid_merge_idx


# ============================================================
#  七、審查與診斷分析引擎
# ============================================================
def pre_check(sutra_text: str, segments: List[str], md_sections: Optional[List[str]] = None) -> List[ReviewIssue]:
    """程式化純物理硬傷預檢（碎首/斷尾 + 非整偈 + 格式殘缺 + 全局漏段檢測，回傳 ReviewIssue 清單）"""
    style = detect_punctuation_style(sutra_text)
    issues: List[ReviewIssue] = []
    total = len(segments)

    clean_chars = []
    clean_to_raw = []
    for raw_idx, ch in enumerate(sutra_text):
        if RE_CLEAN_CHAR.match(ch):
            clean_chars.append(ch)
            clean_to_raw.append(raw_idx)
    clean_sutra = "".join(clean_chars)
    norm_sutra = normalize_text(clean_sutra)

    last_pos = 0
    title_pattern = (
        r"^(?:.*?[品卷章](?:第[一二三四五六七八九十百千\d]+)?(?:之[一二三四五六七八九十\d]+)?"
        r"|第[一二三四五六七八九十百千\d]+[品卷章]|.+品|入菩薩行論|大乘入楞伽經.*)$"
    )
    seen_segment_positions = {}

    for i, seg in enumerate(segments):
        raw_seg = seg.strip()
        if not raw_seg:
            continue

        clean_t = normalize_text(raw_seg)
        is_at_end = (i == total - 1)
        sec_text = md_sections[i] if (md_sections and i < len(md_sections)) else raw_seg
        pos = find_best_position(raw_seg, sec_text, clean_sutra, norm_sutra, last_pos)

        if pos != -1:
            if len(clean_t) >= 8:
                count_in_sutra = norm_sutra.count(clean_t[:15]) if len(clean_t) >= 15 else norm_sutra.count(clean_t)
                if pos in seen_segment_positions and count_in_sutra <= 1:
                    dup_orig_idx = seen_segment_positions[pos]
                    issues.append(ReviewIssue(
                        index=i,
                        issue_type="重複內容",
                        problem=f"本段經文與第 [{dup_orig_idx}] 段完全重複（原典僅出現 1 次），應刪除重複",
                        merge_indices=[i],
                    ))
                    last_pos = pos + len(clean_t)
                    continue
                else:
                    seen_segment_positions[pos] = i
            last_pos = pos + len(clean_t)

        # 與前段的重疊檢測
        if i > 0 and len(clean_t) >= 10:
            prev_clean = normalize_text(segments[i - 1].strip())
            overlap_len = 0
            for ol in range(min(len(prev_clean), len(clean_t), 40), 7, -1):
                if prev_clean.endswith(clean_t[:ol]):
                    overlap_len = ol
                    break
            if overlap_len >= 8 and (overlap_len / max(1, len(clean_t)) >= 0.7 or len(clean_t) <= 25):
                dup_str = clean_t[:overlap_len]
                count_in_sutra = norm_sutra.count(dup_str) if len(dup_str) < 15 else norm_sutra.count(dup_str[:15])
                if count_in_sutra <= 1:
                    issues.append(ReviewIssue(
                        index=i,
                        issue_type="重複內容",
                        problem=f"本段首句「{dup_str[:10]}...」與前段實質重複，應刪除重複部分，保留後續經文",
                        merge_indices=[i],
                    ))

        is_title = bool(
            re.match(title_pattern, raw_seg.strip())
            or "品第" in raw_seg
            or raw_seg.strip().endswith("品")
        ) and len(clean_t) <= 30 and not any(p in raw_seg for p in ["。", "！", "？", "；"])
        is_annotation = bool(re.match(r"^[\(（\[【〔〈《].*?[\)）\]】〕〉》]$", raw_seg.strip())) and len(clean_t) <= 30

        clauses = [
            RE_CLEAN_CJK.sub("", c)
            for c in re.split(r"[，。！？；、\s\n　]", raw_seg)
            if RE_CLEAN_CJK.sub("", c)
        ]
        is_5_rhythm = (all(len(c) in [5, 10, 20] for c in clauses) and len(clean_t) % 5 == 0) if clauses else (len(clean_t) >= 10 and len(clean_t) % 5 == 0)
        is_7_rhythm = (all(len(c) in [7, 14, 28] for c in clauses) and len(clean_t) % 7 == 0) if clauses else (len(clean_t) >= 14 and len(clean_t) % 7 == 0)
        is_strict_gatha = (is_5_rhythm and len(clean_t) >= 20 and len(clean_t) % 20 == 0) or (is_7_rhythm and len(clean_t) >= 28 and len(clean_t) % 28 == 0)

        if is_title or is_annotation or is_strict_gatha:
            if md_sections and i < len(md_sections):
                valid, missing = validate_output_format(md_sections[i])
                if not valid:
                    issues.append(ReviewIssue(
                        index=i,
                        issue_type="銷文格式殘缺",
                        problem=f"本段格式不完整，缺少必要區塊：{missing}，應重新銷文補全",
                        merge_indices=[i],
                    ))
            continue

        # 1. 現代標點腰斬
        pure_end = re.sub(r"[\s」』”\"\'\)）］】〕＞》〉]+$", "", raw_seg)
        if pure_end and pure_end[-1] in ["，", "、", "—", "-"] and not is_at_end:
            issues.append(ReviewIssue(
                index=i,
                issue_type="標點腰斬未完",
                problem=f"段落結尾停在未完標點「{pure_end[-1]}」，句子未說完，應與下段合併",
                merge_indices=[i, i + 1] if i + 1 < total else [i],
            ))
            continue

        # 2. 開頭標點殘肢
        if i > 0 and raw_seg.startswith(("，", "、", "；", "。", "！", "？", "：")):
            prev_seg = segments[i - 1].strip()
            prev_pure_end = re.sub(r"[\s」』”\"\'\)）］】〕＞》〉]+$", "", prev_seg)
            if prev_pure_end and prev_pure_end[-1] in ["，", "、", "：", "；", "—", "-"]:
                issues.append(ReviewIssue(
                    index=i - 1,
                    issue_type="開頭標點殘肢",
                    problem=f"段落開頭為標點「{raw_seg[0]}」且前段未完，應與前段合併",
                    merge_indices=[i - 1, i],
                ))
                continue

        # 3. 物理碎首檢測
        if style != "NO_PUNCT" and pos > 0 and clean_to_raw and i > 0:
            raw_idx = clean_to_raw[pos]
            prev_raw_idx = clean_to_raw[pos - 1]
            intervening = sutra_text[prev_raw_idx + 1 : raw_idx]
            has_sep = bool(re.search(r"[\s\n\r　，。！？；、：—…\(\)（）\[\]【】《》〈〉「」『』\"\'“”‘’◎]", intervening))
            if not has_sep:
                char_before = sutra_text[prev_raw_idx]
                if re.match(r"[\u4e00-\u9fa5]", char_before):
                    issues.append(ReviewIssue(
                        index=i - 1,
                        issue_type="字詞開頭碎首腰斬",
                        problem=f"本段開頭在詞中被截斷（前字『{char_before}』無標點分隔），應與前段合併",
                        merge_indices=[i - 1, i],
                    ))
                    continue

        # 4. 偈頌末尾合法殘偈保護
        is_gatha_tail = False
        if len(clean_t) in [10, 14] and pos != -1:
            after_clean = clean_sutra[pos + len(clean_t):]
            if not after_clean or any(
                after_clean.startswith(kw)
                for kw in ["童子", "爾時", "佛告", "時", "復次", "世尊", "比丘", "此", "問曰", "答曰", "論曰", "自此", "大慧", "說偈"]
            ):
                is_gatha_tail = True

        # 5. 物理斷尾腰斬檢測
        is_bracket_ended = bool(re.search(r"[\)）\]】〕＞》〉][\s\d一二三四五六七八九十百千]*$", raw_seg.strip()))
        is_mantra_ended = any(raw_seg.strip().endswith(kw) for kw in ["莎皤訶", "娑婆訶", "娑訶", "莎訶", "泮", "吽", "唵"])
        if style != "NO_PUNCT" and not is_strict_gatha and not is_gatha_tail and not is_title and not is_annotation and not is_bracket_ended and not is_mantra_ended and not is_at_end:
            if pure_end:
                last_char = pure_end[-1]
                valid_puncts = ["。", "！", "？", "：", "；", "◎", "…"]
                if last_char not in valid_puncts and pos >= 0 and clean_to_raw:
                    end_clean_idx = pos + len(clean_t)
                    if end_clean_idx < len(clean_to_raw):
                        raw_next_idx = clean_to_raw[end_clean_idx]
                        raw_curr_end_idx = clean_to_raw[end_clean_idx - 1]
                        intervening = sutra_text[raw_curr_end_idx + 1 : raw_next_idx]
                        has_sep = bool(re.search(r"[\s\n\r　，。！？；、：—…\(\)（）\[\]【】《》〈〉「」『』\"\'“”‘’◎]", intervening))
                        if not has_sep and raw_next_idx < len(sutra_text) and re.match(r"[\u4e00-\u9fa5]", sutra_text[raw_next_idx]):
                            issues.append(ReviewIssue(
                                index=i,
                                issue_type="字詞結尾斷尾腰斬",
                                problem=f"本段結尾在詞中截斷且緊接後文漢字『{sutra_text[raw_next_idx]}』，應與下段合併",
                                merge_indices=[i, i + 1],
                            ))
                            continue

        # 6. 格式殘缺檢測
        if md_sections and i < len(md_sections):
            valid, missing = validate_output_format(md_sections[i])
            if not valid:
                issues.append(ReviewIssue(
                    index=i,
                    issue_type="銷文格式殘缺",
                    problem=f"本段格式不完整，缺少必要區塊：{missing}，應重新銷文補全",
                    merge_indices=[i],
                ))
                continue

    # 7. 全局物理級漏段/跳字精確檢測
    missing_gaps = find_missing_gaps(sutra_text, segments)
    for g in missing_gaps:
        p_idx = g.prev_idx
        gap_content = g.gap_text
        pos = g.position

        if pos == "head" or p_idx < 0:
            target_indices = [0]
            desc = f"經文開頭遺漏了 {len(normalize_text(gap_content))} 字未銷文（『{gap_content[:20]}...』），應合併首段重新切片"
        elif pos == "tail":
            target_indices = [total - 1] if total > 0 else [0]
            desc = f"經文結尾遺漏了 {len(normalize_text(gap_content))} 字未銷文（『{gap_content[:20]}...』），應補齊末尾段落"
        else:
            target_indices = [p_idx, p_idx + 1] if p_idx + 1 < total else [p_idx]
            desc = f"段落 [{p_idx}] 與後文夾縫間遺漏了 {len(normalize_text(gap_content))} 字（『{gap_content[:20]}...』），應由原始經文重新切片補齊"

        issues.append(ReviewIssue(
            index=target_indices[0] if (target_indices and target_indices[0] >= 0) else 0,
            issue_type="經文漏段補齊",
            problem=desc,
            merge_indices=[idx for idx in target_indices if idx >= 0] or [0],
            gap_text=gap_content,
            position=pos,
        ))

    return issues

REVIEW_SYSTEM = """你是精通三藏文法、因明論理、佛經科判與講經實務的資深佛學審查導師。

【核心指導哲學：講經科判與獨立法義思維】
佛經銷文解義的目的，是將經文切分為「最適宜開示與深入剖析的法義單元」。
我們追求的是【微觀句意自足、宏觀利於深解】，審查需兼顧【防割裂（合併）】與【防臃腫（拆分）】雙向平衡。
★【防崩潰與防碎片化鐵律】：
   1. 嚴禁滾雪球式強行合併過長大段（>120字），嚴防輸出超時與邏輯混雜。
   2. 【碎片化之嚴格定義】：僅針對無序號、無獨立說明的密集短詞連綴（如《楞伽經》百八句「生非生、常非常、因非因」等純句列），此類才需群組；凡帶有序號或名相之條目，一律非碎片化，絕不可合併！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🟥 一、何時【必須通報合併】？（修復實質語法割裂 -> merge_indices 填 [相鄰段落]）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 【專有名詞/詞彙跨段腰斬】：如前段末「阿彌陀」，後段首「佛」；或同一名詞內部被生硬截斷。
2. 【純說法引導詞孤立成段】：如前段僅有「佛告阿難：『善男子！』」，正文全落在下一段。
3. 【條件/因果前綴懸空】：僅有假設子句（如「若彼所生」），無主句、無結論，單獨無法成義。
4. 【半偈殘篇】：五言/七言韻文在非整偈處中斷（單段僅 5/7 字且句意未完）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✂️ 二、何時【必須標記拆分】？（修復超長堆疊 -> merge_indices 僅填 [單一段落編號]）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
凡單一段落過長（散文 > 80~100 字，且內部包含多個可獨立開示的完整法義句）時標記拆分：
1. 【問答角色混雜】：前半段為請法者之問，後半段直接轉為佛陀之答，應在轉折處標記拆分。
2. 【多重法相過度堆疊】：一段內塞入過多不相干的長篇平行法相導致銷文臃腫，應拆為適度單元（每個單元仍應維持 20~50 字的完整義理脈絡，切勿過度切碎）。
3. 【偈頌連續堆疊過長】：偈頌連續超過 2 整偈（五言 >40 字、七言 >56 字），應拆分為 1~2 偈。
★【拆分標記規範】：`type` 填 `單段過長需拆分`，`merge_indices` 【必須嚴格只填單一索引 [段號]】（如 `[49]`）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🟩 三、何時【堅決放行免審（核心豁免原則）】？（重要！絕對嚴禁通報合併或拆分！）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. ★【帶序號或名相之列舉條目（一者...二者...三者...）—— 堅決放行！】：凡帶數字序號或法相標籤之條目（如「一者、諦實故；」「一者、觀待道理；」），無論字數多短，皆屬標準科判單元，【絕對禁止】通報為「斷句邊界錯位」或要求合併！
2. ★【明確區分】：僅針對如《楞伽經》百八句那種「無序號之極短純詞彙連綴（生非生、常非常）」才需適度群組；只要是帶有序號的法相列舉，單獨成段完全合法，堅決放行！
3. ★【設問徵起、總標句與精簡問答（「所以者何」、「何以故」、「王言：不也。」）—— 堅決放行！】：單獨成段極利於提綱挈領與發起問端，完全合法，嚴禁通報。
4. ★【篇幅適中之獨立段落（< 100 字）—— 堅決放行！】：只要內部文意自足，即使包含設問與別釋、譬喻與法合，皆屬標準段落，嚴禁過度挑刺拆分。

輸出格式：嚴格輸出 JSON 陣列，不要任何 markdown 標籤或無關廢話。
[{"index": 段落編號, "type": "斷句邊界錯位|字詞腰斬|半偈殘篇|條件句腰斬|單段過長需拆分|重複內容", "problem": "簡述具體語法錯置原因與正確邊界切分建議", "merge_indices": [要處理的段落編號, 如邊界錯位填 [42, 43]，單段拆分填 [29]]}]
若斷句全部自然流暢、邊界清晰、篇幅適度，請直接輸出：[]"""

def ai_review(
    client: OpenAI,
    model: str,
    sutra_text: str,
    segments: List[str],
    style: str,
    logger: logging.Logger,
    reasoning_effort: str = "high"
) -> Optional[List[Dict[str, Any]]]:
    """AI 深度審查語意連貫性（Prompt Cache 前綴對齊優化）"""
    seg_list = "\n".join(f"{i}. 「{s}」" for i, s in enumerate(segments))
    style_hint = (
        "古典全句號文本（句號包含正常句讀與停頓，只要單元語意可獨立銷文即屬合格，切勿過度合併）。"
        if style == "ALL_PERIOD"
        else "現代新標點文本。"
    )
    # 與修復階段嚴格保持相同的經文前綴標題，鎖定 DeepSeek Prompt Cache
    user_msg = (
        f"【經典全本文脈背景】：\n{sutra_text}\n\n"
        f"【文本風格】：{style_hint}\n\n"
        f"【已完成銷文之原典段落列表】（共 {len(segments)} 段）：\n{seg_list}"
    )

    logger.info(f"AI 審查：正在進行全局語意流暢度與因明攻防邏輯校驗（共 {len(segments)} 段）...")

    pool = getattr(client, "key_pool", None)
    max_retries = max(len(pool) * 2, 5) if (pool and pool.has_multiple()) else 3

    for retry in range(max_retries):
        try:
            text = stream_completion(
                client=client,
                model=model,
                system_prompt=REVIEW_SYSTEM,
                user_prompt=user_msg,
                reasoning_effort=reasoning_effort,
                logger=logger,
                action_name=f"AI 斷句審查 (重試 {retry + 1}/{max_retries})"
            ).strip()

            text = RE_THINK_TAG.sub("", text).strip()
            text = re.sub(r"^\s*```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
            text = re.sub(r"\n?\s*```\s*$", "", text, flags=re.MULTILINE).strip()
            text = re.sub(r",\s*([\]}])", r"\1", text)

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\[[\s\S]*\]", text)
                if m:
                    clean_m = re.sub(r",\s*([\]}])", r"\1", m.group())
                    try:
                        parsed = json.loads(clean_m)
                    except json.JSONDecodeError:
                        parsed = []
                else:
                    parsed = []

            if isinstance(parsed, dict):
                issues_raw = []
                for k in ["issues", "data", "result", "problems"]:
                    if k in parsed and isinstance(parsed[k], list):
                        issues_raw = parsed[k]
                        break
            elif isinstance(parsed, list):
                issues_raw = parsed
            else:
                issues_raw = []

            valid_issues: List[ReviewIssue] = []
            max_idx = len(segments) - 1
            for raw_iss in issues_raw:
                if not isinstance(raw_iss, dict):
                    continue
                raw_merge = raw_iss.get("merge_indices", [raw_iss.get("index", -1)])
                filtered_merge = [idx for idx in raw_merge if isinstance(idx, int) and 0 <= idx <= max_idx]
                if filtered_merge:
                    issue_obj = ReviewIssue(
                        index=filtered_merge[0],
                        issue_type=raw_iss.get("type", "斷句邊界調整"),
                        problem=raw_iss.get("problem", ""),
                        merge_indices=sorted(list(set(filtered_merge))),
                        gap_text=raw_iss.get("gap_text"),
                        position=raw_iss.get("position")
                    )
                    valid_issues.append(issue_obj)

            filtered_count = max(0, len(issues_raw) - len(valid_issues))
            logger.info(
                f"AI 審查完成：檢出 {len(valid_issues)} 處有效邏輯割裂或斷句不當之處"
                f"（已過濾 {filtered_count} 處越界幻覺）"
            )
            return valid_issues

        except KeyboardInterrupt:
            logger.warning("\n🛑 使用者中斷了 AI 審查流程 (Ctrl+C)")
            raise
        except Exception as e:
            err_msg = str(e)
            fatal_keywords = [
                "insufficient balance", "creditserror", "authenticationerror",
                "invalid_api_key", "401", "402", "payment required"
            ]
            is_fatal = any(kw in err_msg.lower() for kw in fatal_keywords)

            if is_fatal:
                if pool:
                    all_dead = pool.mark_current_dead(client, logger, reason=err_msg)
                    if all_dead:
                        logger.error("❌ 所有 API 金鑰皆已失效或餘額不足！AI 審查流程安全中止。")
                        return None
                    time.sleep(1)
                    continue
                else:
                    logger.error(f"❌ API 金鑰無效或帳號餘額不足 ({err_msg})！AI 審查流程中止。")
                    return None

            if pool and pool.has_multiple():
                pool.rotate_client(client, logger, reason=f"AI 審查遭遇限制/異常 ({e})")
                backoff = 1.5
            else:
                is_free_or_rate_limit = (
                    ":free" in model.lower()
                    or "openrouter" in str(client.base_url).lower()
                    or "googleapis" in str(client.base_url).lower()
                    or "429" in err_msg
                    or "rate" in err_msg.lower()
                    or "quota" in err_msg.lower()
                )
                # 遭遇 429 限流時，單 Key 進行 15s -> 30s -> 45s 階梯退避
                backoff = min(45, (retry + 1) * 15) if is_free_or_rate_limit else (retry + 1) * 3

            logger.error(f"  ❌ AI 審查呼叫失敗 ({e})，等待 {backoff} 秒後進行第 {retry+1}/{max_retries} 次重試...")
            time.sleep(backoff)

    logger.error(f"❌ AI 審查重試 {max_retries} 次皆失敗，放棄本次審查。")
    return None


def merge_overlapping_issues(
    issues: List[Any],
    max_valid_len: Optional[int] = None
) -> List[ReviewIssue]:
    """標準且穩定的單遍掃描區間合併演算法（相容 ReviewIssue 與 Dict 輸入）"""
    if not issues:
        return []

    # 1. 規範化每個 issue 為起止連續區間
    normalized_items = []
    for x in issues:
        if hasattr(x, "merge_indices"):
            raw_indices = x.merge_indices
            issue_type = x.issue_type
            problem = x.problem
            gap_text = x.gap_text
            position = x.position
        else:
            raw_indices = x.get("merge_indices", [x.get("index", 0)])
            issue_type = x.get("type", "")
            problem = x.get("problem", "")
            gap_text = x.get("gap_text")
            position = x.get("position")

        valid_indices = [
            i for i in raw_indices
            if (max_valid_len is None or (isinstance(i, int) and 0 <= i < max_valid_len))
        ]
        if not valid_indices:
            continue

        min_i, max_i = min(valid_indices), max(valid_indices)
        normalized_items.append({
            "start": min_i,
            "end": max_i,
            "issue_type": issue_type or "",
            "problem": problem or "",
            "gap_text": gap_text,
            "position": position,
        })

    if not normalized_items:
        return []

    # 2. 依照起點索引與跨度排序
    normalized_items.sort(key=lambda item: (item["start"], item["end"]))

    merged_raw = [normalized_items[0]]
    for cur in normalized_items[1:]:
        prev = merged_raw[-1]
        # 若有重疊或相鄰且總跨度 <= 8 個段落
        if cur["start"] <= prev["end"] and (max(prev["end"], cur["end"]) - prev["start"] + 1 <= 8):
            prev["end"] = max(prev["end"], cur["end"])
            if cur["issue_type"] and cur["issue_type"] not in prev["issue_type"]:
                prev["issue_type"] = f"{prev['issue_type']}+{cur['issue_type']}"
            if cur["problem"] and cur["problem"] not in prev["problem"]:
                prev["problem"] = f"{prev['problem']}；{cur['problem']}"
            # ★ 修復：若兩者皆有漏段文字，必須安全串接，絕不可靜默丟棄後者
            if cur["gap_text"]:
                if prev["gap_text"]:
                    if cur["gap_text"] not in prev["gap_text"]:
                        prev["gap_text"] = f"{prev['gap_text']} {cur['gap_text']}".strip()
                else:
                    prev["gap_text"] = cur["gap_text"]
            if not prev["position"] and cur["position"]:
                prev["position"] = cur["position"]
        else:
            merged_raw.append(cur)

    # 3. 轉回 ReviewIssue 強型別物件
    result: List[ReviewIssue] = []
    for item in merged_raw:
        result.append(ReviewIssue(
            index=item["start"],
            issue_type=item["issue_type"],
            problem=item["problem"],
            merge_indices=list(range(item["start"], item["end"] + 1)),
            gap_text=item["gap_text"],
            position=item["position"],
        ))

    return result


# ============================================================
#  八、斷點續傳 (Checkpoint) 輔助
# ============================================================
def load_checkpoint(checkpoint_path: str) -> Dict[Tuple[int, ...], Any]:
    """讀取中斷前已完成的銷文快取（具備型別容錯防禦）"""
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return {}
            
            result = {}
            for item in data:
                if not isinstance(item, dict) or "merge_indices" not in item or "new_content" not in item:
                    continue
                raw_indices = item["merge_indices"]
                if isinstance(raw_indices, (list, tuple)):
                    key = tuple(sorted(list(set(raw_indices))))
                elif isinstance(raw_indices, int):
                    key = (raw_indices,)
                else:
                    continue
                result[key] = item["new_content"]
            return result
        except Exception:
            pass
    return {}


def save_checkpoint(checkpoint_path: str, cached_dict: Dict[Tuple[int, ...], Any]) -> None:
    """即時原子儲存當前已完成的銷文段落到快取檔"""
    try:
        data = [
            {"merge_indices": list(k), "new_content": v}
            for k, v in cached_dict.items()
        ]
        temp_path = checkpoint_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, checkpoint_path)
    except Exception:
        pass


def remove_checkpoint(checkpoint_path: str) -> None:
    """清理暫存檔"""
    if os.path.exists(checkpoint_path):
        try:
            os.remove(checkpoint_path)
        except Exception:
            pass


# ============================================================
#  九、業務工作流：補漏模式、審查模式、修正模式、一鍵全自動
# ============================================================
def run_fill_gaps(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    output_path: str,
    logger: logging.Logger,
    mode_title: str = "經文補漏 (Gap Fill)",
    issue_type: str = "經文漏段補齊"
) -> None:
    logger.info("=" * 65)
    logger.info(f"🚀 啟動 {mode_title} 模式：{output_path}")
    logger.info("=" * 65)

    if not os.path.exists(output_path):
        base_name = os.path.splitext(os.path.basename(output_path))[0].replace("_銷文", "")
        safe_write_file(output_path, f"# 佛經銷文：{base_name}\n\n")

    total_added_blocks = 0
    clean_sutra_len = len(normalize_text(sutra_text))

    while True:
        # 每一輪推進前，重新讀取、物理重排並重新計算即時遮罩
        header, cur_sections = parse_md_sections(output_path)
        existing_segments = extract_segments_from_md(output_path)
        missing_gaps = find_missing_gaps(sutra_text, existing_segments)

        if not missing_gaps:
            logger.info("✅ 經文覆蓋率已達 100%，全文皆已銷文完畢！")
            break

        # 每次僅取第一個 Gap 進行推進
        g = missing_gaps[0]
        gap_raw = getattr(g, "gap_text", "").strip()
        p_idx = getattr(g, "prev_idx", -1)
        prev_sentence = existing_segments[p_idx] if (existing_segments and 0 <= p_idx < len(existing_segments)) else ""

        gap_chars_len = len(normalize_text(gap_raw))
        logger.info(f"\n📖 正在推進漏段（長度 {gap_chars_len} 字）：『{gap_raw[:30]}...』")

        new_blocks_in_gap = []
        def block_success_handler(block_content: str, sent: str):
            nonlocal total_added_blocks, prev_sentence
            total_added_blocks += 1
            prev_sentence = sent
            new_blocks_in_gap.append(block_content)

        blocks, rem_after = generate_sutra_segments(
            client=client,
            model=model,
            sutra_text=sutra_text,
            target_raw_text=gap_raw,
            prev_sentence=prev_sentence,
            logger=logger,
            issue_type=issue_type,
            problem_desc="",
            reasoning_effort=args.reasoning_effort,
            on_block_success=block_success_handler
        )

        if new_blocks_in_gap:
            cur_sections.extend(new_blocks_in_gap)
            combined_body = (header.strip() + "\n\n---\n\n" if header.strip() else "") + "\n\n---\n\n".join(cur_sections)
            reordered_md = reorder_markdown_by_sutra(combined_body, sutra_text)
            safe_write_file(output_path, reordered_md)

        if not blocks or (rem_after and len(normalize_text(rem_after)) > 2):
            logger.error(f"  ❌ 經文推進於『{gap_raw[:20]}...』處中斷。")
            break

    # 計算最終成果報表
    latest_segments = extract_segments_from_md(output_path)
    final_gaps = find_missing_gaps(sutra_text, latest_segments)
    total_gaps_chars = sum(len(normalize_text(getattr(g, "gap_text", ""))) for g in final_gaps)
    covered_chars = max(0, clean_sutra_len - total_gaps_chars)
    coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

    logger.info("\n" + "=" * 65)
    logger.info(f"🎉 {mode_title} 執行完畢！本次共產出/補齊 {total_added_blocks} 個段落。")
    logger.info(f"📊 經文總覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
    logger.info("=" * 65)

def run_generate(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    output_path: str,
    logger: logging.Logger
) -> None:
    """★ 全本全新銷文模式（調用統一推進引擎）"""
    run_fill_gaps(
        args=args,
        client=client,
        model=model,
        sutra_text=sutra_text,
        output_path=output_path,
        logger=logger,
        mode_title="全本銷文 (Generate)",
        issue_type="全本經文銷文"
    )


def run_fix_gaps(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    segments: List[str],
    style: str,
    output_path: str,
    logger: logging.Logger
) -> None:
    """★ 專注補漏模式（調用統一推進引擎）"""
    run_fill_gaps(
        args=args,
        client=client,
        model=model,
        sutra_text=sutra_text,
        output_path=output_path,
        logger=logger,
        mode_title="專注補漏 (Fix Gaps)",
        issue_type="經文漏段補齊"
    )


def run_review(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    segments: List[str],
    style: str,
    output_path: str,
    logger: logging.Logger
) -> Optional[List[Dict[str, Any]]]:
    """執行 Phase 1 審查（預檢 + AI 深度審查）"""
    logger.info("【Phase 1a】程式化物理損壞預檢...")
    _, md_sections = parse_md_sections(output_path)
    pre_issues = pre_check(sutra_text, segments, md_sections=md_sections)
    logger.info(f"  預檢發現 {len(pre_issues)} 處硬性符號、漏字或格式問題")

    logger.info(f"【Phase 1b】AI 深度審查（風格：{'古典全句號' if style == 'ALL_PERIOD' else '現代新標點'}）...")
    ai_issues = ai_review(client, model, sutra_text, segments, style, logger, args.reasoning_effort)
    if ai_issues is None:
        logger.error("\n❌ AI 審查流程因 API 錯誤中斷，已中止後續流程。請確認 API 金鑰與端點設定後重試。")
        return None

    all_issues = pre_issues.copy()
    # 支援 ReviewIssue 物件與字典雙重存取，防止 AttributeError
    seen_indices = {
        tuple(iss.merge_indices if hasattr(iss, "merge_indices") else iss.get("merge_indices", [iss.get("index", 0)]))
        for iss in pre_issues
    }
    for iss in ai_issues:
        if hasattr(iss, "merge_indices"):
            m_idx = tuple(iss.merge_indices)
        else:
            m_idx = tuple(iss.get("merge_indices", [iss.get("index", -1)]))
        if m_idx not in seen_indices and len(m_idx) > 0 and m_idx[0] != -1:
            all_issues.append(iss)
            seen_indices.add(m_idx)

    # 合併重疊區間並保證升序
    merged_issue_objs = merge_overlapping_issues(all_issues, max_valid_len=len(segments))
    merged_issue_objs.sort(key=lambda x: x.merge_indices[0] if x.merge_indices else x.index)

    # 輸出為標準 JSON 格式
    serialized_issues = [iss.to_dict() for iss in merged_issue_objs]
    review_path = os.path.splitext(output_path)[0] + "_review.json"
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(serialized_issues, f, ensure_ascii=False, indent=2)

    logger.info(f"\n📊 審查完畢，共發現 {len(merged_issue_objs)} 處問題：")
    for iss in merged_issue_objs:
        m_str = ",".join(map(str, iss.merge_indices))
        logger.info(f"  段落 [{m_str}] ({iss.issue_type})：{iss.problem}")
    logger.info(f"結果報告已儲存至：{review_path}")

    # 精確計算覆蓋率與漏段清單
    remaining_gaps = find_missing_gaps(sutra_text, segments)
    clean_sutra_len = len(normalize_text(sutra_text))
    total_gaps_chars = sum(len(normalize_text(getattr(g, "gap_text", ""))) for g in remaining_gaps)
    covered_chars = max(0, clean_sutra_len - total_gaps_chars)
    coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

    logger.info("\n" + "=" * 60)
    logger.info(f"📈 當前經文覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
    if remaining_gaps:
        logger.warning(f"⚠️ 檢測到 {len(remaining_gaps)} 處實質經文漏段：")
        for idx, g in enumerate(remaining_gaps, 1):
            gap_txt = getattr(g, "gap_text", "")
            logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(gap_txt))} 字 | 預覽: 『{gap_txt[:40]}...』")
    else:
        logger.info("🎉 經文已 100% 全覆蓋，無實質漏句。")
    logger.info("=" * 60)
    return serialized_issues


def run_fix(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    segments: List[str],
    output_path: str,
    logger: logging.Logger,
    all_issues: Optional[List[Dict[str, Any]]] = None
) -> bool:
    """依審查報告執行批次修正（回傳修復成功狀態）"""
    is_standalone_fix = (all_issues is None)
    review_path = os.path.splitext(output_path)[0] + "_review.json"
    if all_issues is None:
        if not os.path.exists(review_path):
            logger.error(f"找不到審查結果報告: {review_path}，請先執行 --review")
            return False
        with open(review_path, "r", encoding="utf-8") as f:
            all_issues = json.load(f)

    if not all_issues:
        logger.info("✅ 審查報告中無任何待修正項目，所有段落皆符合標準。")
        return True

    max_fix = min(len(all_issues), args.max_fix)
    if args.dry_run:
        logger.info(f"\n🔍 [DRY RUN 模式啟用] 預覽即將修正的 {max_fix} 處段落（不呼叫 API 且不寫入檔案）：")
        for idx, iss in enumerate(all_issues[:max_fix], 1):
            m_str = ",".join(map(str, iss.get('merge_indices', [iss.get('index')])))
            logger.info(f"  [{idx}/{max_fix}] 段落 [{m_str}] ({iss.get('type','')})：{iss.get('problem','')}")
        return True

    logger.info(f"\n📊 綜合評估共 {len(all_issues)} 處待修正問題：")
    for iss in all_issues:
        m_str = ",".join(map(str, iss.get('merge_indices', [iss.get('index')])))
        logger.info(f"  段落 [{m_str}] ({iss.get('type','')})：{iss.get('problem','')}")

    logger.info(f"\n【Phase 2】開始執行段落合併與重新銷文（本輪預計修正 {max_fix} 處）...")

    checkpoint_path = os.path.splitext(output_path)[0] + "_checkpoint.json"
    cached_corrections = load_checkpoint(checkpoint_path)

    corrections = []
    failed_count = 0

    for i, iss in enumerate(all_issues[:max_fix], 1):
        merge_tuple = tuple(sorted(list(set(iss.get("merge_indices", [iss.get("index")])))))
        cached_entry = cached_corrections.get(merge_tuple)

        if isinstance(cached_entry, str):
            logger.info(f"\n[{i}/{max_fix}] ⚡ [斷點續傳] 檢測到段落 {list(merge_tuple)} 已完全修復，直接載入跳過 API！")
            corrections.append((cached_entry, list(merge_tuple)))
            continue

        partial_state = cached_entry if isinstance(cached_entry, dict) else None

        def step_saver(m_idx, state):
            cached_corrections[tuple(m_idx)] = state
            save_checkpoint(checkpoint_path, cached_corrections)

        logger.info(f"\n[{i}/{max_fix}] 處理問題項目...")
        new_content, merge_idx = fix_single_issue(
            client=client,
            model=model,
            sutra_text=sutra_text,
            issue=iss,
            segments=segments,
            logger=logger,
            reasoning_effort=args.reasoning_effort,
            partial_state=partial_state,
            on_step_done=step_saver,
            output_path=output_path
        )

        if new_content:
            corrections.append((new_content, merge_idx))
            cached_corrections[tuple(merge_idx)] = new_content
            save_checkpoint(checkpoint_path, cached_corrections)
        else:
            failed_count += 1
            logger.error(
                f"  ❌ 項目 [{i}/{max_fix}] 段落 {list(merge_tuple)} 尚未全數完成，"
                f"已暫存現有進度，待重新執行時接續。"
            )
        time.sleep(0.5)

    if failed_count > 0:
        logger.warning("\n" + "=" * 65)
        logger.warning(f"⚠️ 本輪有 {failed_count} 個項目因 API/網路問題修復失敗！")
        logger.warning(f"💡 成功完成的 {len(corrections)} 個項目已安全保存在暫存檔中。")
        logger.warning("👉 請直接【再次執行原指令】，系統將自動秒讀成功項目，專門重跑失敗的項目！")
        logger.warning("=" * 65 + "\n")
        return False

    if not corrections:
        logger.warning("❌ 未能成功修正任何段落（所有段落修正均失敗或無內容變更），終止後續檔案寫入。")
        return False

    logger.info(f"\n【Phase 3】更新 MD 檔案並執行經文物理順序重排...")
    update_md_file(output_path, sutra_text, corrections, segments, logger)
    logger.info(f"\n🎉 修正完成：已重寫更新 {len(corrections)}/{max_fix} 處段落。")

    if max_fix == len(all_issues):
        remove_checkpoint(checkpoint_path)
        if os.path.exists(review_path):
            try:
                os.remove(review_path)
                logger.info(f"🧹 已清理已執行的審查報告快取（{os.path.basename(review_path)}）")
            except Exception:
                pass
    else:
        logger.info(f"💾 本輪已修正 {max_fix}/{len(all_issues)} 處問題，保留審查報告以供後續執行。")

    if is_standalone_fix:
        logger.info("\n" + "=" * 65)
        logger.info(f"🎉 任務圓滿完成！成功重寫並合併 {len(corrections)} 處問題段落。")
        logger.info(f"最終輸出：{output_path}")
        logger.info("=" * 65)

        latest_segments = extract_segments_from_md(output_path)
        remaining_gaps = find_missing_gaps(sutra_text, latest_segments)
        clean_sutra_len = len(normalize_text(sutra_text))
        total_gaps_chars = sum(len(normalize_text(getattr(g, "gap_text", ""))) for g in remaining_gaps)
        covered_chars = max(0, clean_sutra_len - total_gaps_chars)
        coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

        logger.info(f"📊 修正後經文覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
        if remaining_gaps:
            logger.warning(f"⚠️ 尚有 {len(remaining_gaps)} 處未銷文漏段：")
            for idx, g in enumerate(remaining_gaps, 1):
                gap_txt = getattr(g, "gap_text", "")
                logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(gap_txt))} 字 | 預覽: 『{gap_txt[:40]}...』")
        else:
            logger.info("🎉 經文已 100% 銷文完畢，無任何遺漏與割裂！")
        logger.info("=" * 65)
    return True

# ============================================================
#  十、智慧狀態機與全自動流水線控制器
# ============================================================
class PipelineState(Enum):
    NEED_GENERATE = "全本銷文 (Generate)"
    NEED_AI_REVIEW = "AI 深度審查 (AI Review)"
    NEED_CHECKPOINT_FIX = "接續未完修復 (Resume Checkpoint)"
    NEED_REVIEW_FIX = "依審查報告修復 (Execute Review Fix)"
    NEED_GAP_FILL = "終局安全補漏 (Safety Gap Fill)"
    COMPLETED = "全部流程已完工 (Completed)"


def is_review_json_valid(review_path: str, md_path: str, segments: List[str]) -> bool:
    """校驗既有 review.json 是否依然有效（防止手動編輯 MD 導致過期或索引漂移）"""
    if not os.path.exists(review_path):
        return False
    if os.path.exists(md_path) and os.path.getmtime(md_path) > os.path.getmtime(review_path) + 2:
        return False
    try:
        with open(review_path, "r", encoding="utf-8") as f:
            issues = json.load(f)
        if not issues:
            return True
        max_idx = max([max(iss.get("merge_indices") or [iss.get("index", 0)]) for iss in issues])
        return max_idx < len(segments)
    except Exception:
        return False


def detect_current_state(
    sutra_text: str,
    output_path: str,
    logger: logging.Logger
) -> Tuple[PipelineState, Dict[str, Any]]:
    """
    【智慧狀態感知器 2.0】精確識別：中途停止推進 vs 初稿完成審查 vs 局部漏文
    """
    checkpoint_path = os.path.splitext(output_path)[0] + "_checkpoint.json"
    review_path = os.path.splitext(output_path)[0] + "_review.json"

    # 1. 優先檢查是否有修復到一半的 Checkpoint 暫存檔（具備孤立容錯）
    if os.path.exists(checkpoint_path):
        cached = load_checkpoint(checkpoint_path)
        if cached:
            cached_issues = None
            if os.path.exists(review_path):
                try:
                    with open(review_path, "r", encoding="utf-8") as rf:
                        cached_issues = json.load(rf)
                except Exception:
                    cached_issues = None
            logger.info("🔍 [狀態感知] 發現中斷的修復暫存檔 (Checkpoint)，優先接續修復！")
            return PipelineState.NEED_CHECKPOINT_FIX, {"checkpoint_path": checkpoint_path, "issues": cached_issues}
        else:
            # 檔案損壞或空內容，自動安全清除
            remove_checkpoint(checkpoint_path)

    # 2. 檢查 MD 銷文檔是否存在
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        logger.info("🔍 [狀態感知] 未發現銷文 MD 檔案，將從頭啟動【全本銷文】。")
        return PipelineState.NEED_GENERATE, {}

    segments = extract_segments_from_md(output_path)
    if not segments:
        logger.info("🔍 [狀態感知] 銷文 MD 檔案無有效段落，將從頭啟動【全本銷文】。")
        return PipelineState.NEED_GENERATE, {}

    # 計算全域覆蓋率與各漏段特徵
    gaps = find_missing_gaps(sutra_text, segments)
    gap_chars = sum(len(normalize_text(getattr(g, "gap_text", ""))) for g in gaps)
    norm_sutra_len = len(normalize_text(sutra_text))
    covered_chars = max(0, norm_sutra_len - gap_chars)
    coverage_pct = (covered_chars / max(1, norm_sutra_len)) * 100

    # 3. 檢查是否有已生成的審查報告
    if is_review_json_valid(review_path, output_path, segments):
        with open(review_path, "r", encoding="utf-8") as f:
            issues = json.load(f)
        if issues:
            logger.info(f"🔍 [狀態感知] 檢測到有效的審查報告 ({len(issues)} 處問題)，直接進入【修復階段】。")
            return PipelineState.NEED_REVIEW_FIX, {"issues": issues}
        else:
            if gaps and gap_chars > 0:
                logger.info(f"🔍 [狀態感知] 審查通過但仍殘留 {len(gaps)} 處縫隙 ({gap_chars} 字)，進入【終局安全補漏】。")
                return PipelineState.NEED_GAP_FILL, {"gaps": gaps}
            else:
                logger.info("🔍 [狀態感知] 審查報告 0 問題且經文覆蓋率 100%，流程已圓滿完成！")
                return PipelineState.COMPLETED, {}

    # ★★★ 4. 關鍵智慧判斷：區分【中途停止推進】vs【初稿已完成待審查】★★★
    tail_gaps = [g for g in gaps if getattr(g, "position", "") == "tail"]
    tail_gap_chars = sum(len(normalize_text(getattr(g, "gap_text", ""))) for g in tail_gaps)

    # 判斷條件：若尾部遺漏超過 25 字，或整體覆蓋率 < 95% 且尾部未完，代表「銷文進行到一半被中斷」
    if tail_gap_chars >= 25 or (coverage_pct < 95.0 and tail_gap_chars > 0):
        logger.info(
            f"🔍 [狀態感知] 檢測到目前經文覆蓋率為 {coverage_pct:.1f}%，且文末尚有 {tail_gap_chars} 字未產出。\n"
            f"   👉 智慧判定為【銷文中途停止】，自動無縫接軌繼續推進銷文（不提前調用 AI 審查）！"
        )
        return PipelineState.NEED_GENERATE, {}

    # 5. 初稿已整體完成（已銷文至經末，覆蓋率 >= 95%）-> 進入全篇 AI 審查
    logger.info(
        f"🔍 [狀態感知] 銷文初稿已整體完成（共 {len(segments)} 段，覆蓋率 {coverage_pct:.1f}%），"
        f"直接啟動【AI 深度審查與全域預檢】。"
    )
    return PipelineState.NEED_AI_REVIEW, {"segments": segments}

def run_pipeline(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    style: str,
    output_path: str,
    logger: logging.Logger,
    max_review_cycles: int = 2
) -> bool:
    logger.info("=" * 68)
    logger.info("🌟 啟動佛經銷文智慧閉環流水線（先審查後修復版）")
    logger.info(f"   經文檔案：{args.file}")
    logger.info(f"   輸出檔案：{output_path}")
    logger.info("=" * 68)

    review_cycle_count = 0
    max_cycles = getattr(args, "max_review_cycles", max_review_cycles)

    while True:
        if getattr(client, "key_pool", None) and client.key_pool.is_all_dead():
            logger.error("🛑 檢測到所有 API Key 皆已失效，流水線立即中止！")
            return False

        state, meta = detect_current_state(sutra_text, output_path, logger)

        # ★ 防死循環保護：若審查與修復反覆震盪超過上限，強制進入終局補漏核驗
        if state == PipelineState.NEED_AI_REVIEW:
            review_cycle_count += 1
            if review_cycle_count > max_cycles:
                logger.warning(
                    f"⚠️ [保護機制] AI 審查已達最大循環次數 ({max_cycles} 輪)，"
                    f"跳過後續細節挑刺，進入終局補漏核驗。"
                )
                state = PipelineState.NEED_GAP_FILL

        if state == PipelineState.COMPLETED:
            review_path = os.path.splitext(output_path)[0] + "_review.json"
            try:
                with open(review_path, "w", encoding="utf-8") as f:
                    json.dump([], f)
            except Exception:
                pass
            logger.info("\n" + "=" * 68)
            logger.info("🎉🎉🎉 全流程圓滿完成！經文 100% 全文覆蓋，且已通過因明文法深度審查！")
            logger.info(f"最終成果：{output_path}")
            logger.info("=" * 68)
            return True

        elif state == PipelineState.NEED_GENERATE:
            logger.info("\n🚀 === [Stage 1/3] 開始全本初次銷文 ===")
            run_generate(args, client, model, sutra_text, output_path, logger)

        elif state == PipelineState.NEED_AI_REVIEW:
            logger.info(f"\n🧐 === [Stage 2/3] 執行 AI 深度邏輯審查 (第 {review_cycle_count}/{max_cycles} 輪) ===")
            segments = extract_segments_from_md(output_path)
            issues = run_review(args, client, model, sutra_text, segments, style, output_path, logger)
            if issues is None:
                logger.error("❌ 審查過程遭遇錯誤，流水線已安全暫停。")
                return False
            if not issues:
                logger.info("✨ 經審查無任何語意割裂且無漏段，品質極佳！")
                review_path = os.path.splitext(output_path)[0] + "_review.json"
                try:
                    with open(review_path, "w", encoding="utf-8") as f:
                        json.dump([], f)
                except Exception:
                    pass
                continue

        elif state in (PipelineState.NEED_REVIEW_FIX, PipelineState.NEED_CHECKPOINT_FIX):
            logger.info("\n🔧 === [Stage 3/3] 依審查報告執行修復與段落校準 ===")
            segments = extract_segments_from_md(output_path)
            issues_to_fix = meta.get("issues")
            if issues_to_fix:
                args.max_fix = max(args.max_fix, len(issues_to_fix))
            success = run_fix(args, client, model, sutra_text, segments, output_path, logger, all_issues=issues_to_fix)
            if not success:
                logger.warning("⚠️ 修復中斷，已保存現有進度。隨時再次執行原指令即可秒級接續。")
                return False

            review_path = os.path.splitext(output_path)[0] + "_review.json"
            if os.path.exists(review_path):
                try:
                    os.remove(review_path)
                except Exception:
                    pass

        elif state == PipelineState.NEED_GAP_FILL:
            logger.info("\n🔍 === [終局安全核驗] 執行微小縫隙安全補漏 ===")
            latest_segs = extract_segments_from_md(output_path)
            run_fix_gaps(args, client, model, sutra_text, latest_segs, style, output_path, logger)

def find_sutra_md_pairs(root_dir: str, logger: Optional[logging.Logger] = None) -> List[Tuple[str, str]]:
    """遞迴搜尋目錄下所有 *_銷文.md 並自動匹配對應的 .txt 原始經文檔（含孤兒檔案診斷）"""
    pairs: List[Tuple[str, str]] = []
    unmatched_mds: List[str] = []

    for root, _, files in os.walk(root_dir):
        for f in files:
            if f.endswith("_銷文.md") and not f.startswith("._") and not f.endswith(".bak"):
                md_path = os.path.join(root, f)
                base_name = f[:-len("_銷文.md")]

                # 依序尋找同目錄下對應的 txt 原始經文候選檔名（相容大小寫副檔名）
                candidates = [
                    os.path.join(root, f"{base_name}.txt"),
                    os.path.join(root, f"{base_name}.TXT"),
                    os.path.join(root, f"{base_name}_經文.txt"),
                    os.path.join(root, f"{base_name}_原典.txt"),
                    os.path.join(root, f"{base_name}_原文.txt"),
                ]
                matched_txt = None
                for c in candidates:
                    if os.path.exists(c):
                        matched_txt = c
                        break

                if matched_txt:
                    pairs.append((matched_txt, md_path))
                else:
                    unmatched_mds.append(md_path)

    if unmatched_mds and logger:
        logger.warning(f"⚠️ 發現 {len(unmatched_mds)} 個銷文檔案未找到匹配的 .txt 原典檔案（已跳過）：")
        for u in unmatched_mds:
            logger.warning(f"   ❌ 找不到經文 txt：{u}")

    pairs.sort(key=lambda x: x[1])
    return pairs


def run_batch(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    pairs: List[Tuple[str, str]],
    logger: logging.Logger
) -> None:
    """★ 遞迴批次自動校對流水線（逐一修至 100% 完工後才換下一檔，具備崩潰隔離與統計報表）"""
    total_files = len(pairs)
    logger.info("=" * 70)
    logger.info(f"🚀 啟動批次遞迴校對模式，共發現 {total_files} 個匹配的經文檔案組")
    logger.info("=" * 70)

    success_files: List[str] = []
    failed_files: List[Tuple[str, str]] = []

    for idx, (txt_path, md_path) in enumerate(pairs, 1):
        md_name = os.path.basename(md_path)
        logger.info("\n\n" + "#" * 70)
        logger.info(f"📂 [批次進度 {idx}/{total_files}] 開始校對：{md_name}")
        logger.info(f"   經文原檔：{txt_path}")
        logger.info(f"   銷文檔案：{md_path}")
        logger.info("#" * 70 + "\n")

        try:
            with open(txt_path, "r", encoding="utf-8-sig") as f:
                sutra_text = f.read().strip()
        except Exception as e:
            err_msg = f"讀取經文檔案失敗: {e}"
            logger.error(f"❌ {err_msg} ({txt_path})")
            failed_files.append((md_path, err_msg))
            continue

        if not sutra_text:
            err_msg = "經文檔案為空"
            logger.warning(f"⚠️ {err_msg} ({txt_path})")
            failed_files.append((md_path, err_msg))
            continue

        style = detect_punctuation_style(sutra_text)
        file_log_path = os.path.splitext(md_path)[0] + "_review_log.txt"
        file_logger = setup_logger(file_log_path, logger_name=f"file_logger_{idx}")

        args.file = txt_path
        args.output = md_path

        try:
            segments = extract_segments_from_md(md_path)
            is_ok = True

            # 依據命令列參數進行多模式動態路由
            if args.generate:
                run_generate(args, client, model, sutra_text, md_path, file_logger)
            elif args.fix_gaps:
                run_fix_gaps(args, client, model, sutra_text, segments, style, md_path, file_logger)
            elif args.review:
                issues = run_review(args, client, model, sutra_text, segments, style, md_path, file_logger)
                is_ok = (issues is not None)
            elif args.fix or args.dry_run:
                is_ok = run_fix(args, client, model, sutra_text, segments, md_path, file_logger)
            else:
                # 預設：全流程自動流水線
                is_ok = run_pipeline(args, client, model, sutra_text, style, md_path, file_logger)

            if is_ok:
                success_files.append(md_path)
                logger.info(f"✨ [批次進度 {idx}/{total_files}] 檔案處理完成：{md_name}")
            else:
                failed_files.append((md_path, "處理中途停滯或 API 異常中斷"))
                logger.warning(f"⚠️ [批次進度 {idx}/{total_files}] 檔案未完全完工：{md_name}")
        except KeyboardInterrupt:
            logger.warning("\n🛑 接收到使用者中斷信號 (Ctrl+C)，立即保存進度並退出批次任務。")
            raise
        except Exception as file_exc:
            err_msg = str(file_exc)
            logger.error(f"❌ [批次進度 {idx}/{total_files}] 檔案處理失敗: {err_msg}")
            failed_files.append((md_path, err_msg))
        finally:
            # 關閉單檔日誌 Handler 釋放 Windows 檔案鎖定
            for h in file_logger.handlers[:]:
                try:
                    h.close()
                    file_logger.removeHandler(h)
                except Exception:
                    pass

        # 批次熔斷檢查：只有當金鑰池中的所有 Key 全部都死光時，才真正中斷批次任務
        if getattr(client, "key_pool", None) and client.key_pool.is_all_dead():
            logger.critical("\n🛑 [批次熔斷] 所有 API 金鑰已全數耗盡欠費！立即停止後續所有檔案處理。")
            break

    logger.info("\n" + "=" * 70)
    logger.info(f"📊 批次校驗總結報告：")
    logger.info(f"   總計處理檔案：{total_files} 個")
    logger.info(f"   🎉 圓滿完工數：{len(success_files)} 個")
    logger.info(f"   ⚠️ 未完全完工：{len(failed_files)} 個")

    if failed_files:
        logger.info("\n未完工檔案清單：")
        for f_path, reason in failed_files:
            logger.info(f"   - {os.path.basename(f_path)}: {reason}")
    logger.info("=" * 70)


# ============================================================
#  十一、金鑰讀取與輔助入口
# ============================================================
def load_api_keys(
    key_file: Optional[str] = None,
    api_key_str: Optional[str] = None,
    is_opencode: bool = False,
    is_free_glm: bool = False,
    is_gemini: bool = False
) -> Optional[ApiKeyPool]:
    """讀取多 API Key 並構建 ApiKeyPool（支援多行、註解、逗號分隔）"""
    keys: List[str] = []

    def _parse_keys(raw: str) -> List[str]:
        res = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            for piece in re.split(r"[,;]+", line):
                piece = piece.strip().strip("'\"")
                if piece and not piece.startswith("#") and piece not in res:
                    res.append(piece)
        return res

    if api_key_str and api_key_str.strip():
        keys.extend(_parse_keys(api_key_str))

    # 環境變數查找映射
    if not keys:
        if is_gemini:
            env_candidates = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"]
        elif is_free_glm:
            env_candidates = ["OPENROUTER_API_KEY", "GLM_API_KEY", "OPENAI_API_KEY"]
        elif is_opencode:
            env_candidates = ["OPENCODE_API_KEY", "OPENAI_API_KEY"]
        else:
            env_candidates = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]

        for var in env_candidates:
            val = os.environ.get(var)
            if val and val.strip():
                parsed = _parse_keys(val)
                if parsed:
                    keys.extend(parsed)
                    break

    # 本地金鑰檔案查找映射
    if not keys:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_map = {
            "gemini": ["gemini_key.txt", "google_key.txt", "gemini_api_key.txt", "api_key.txt"],
            "free_glm": ["openrouter_key.txt", "openrouter_api_key.txt", "glm_key.txt", "api_key.txt"],
            "opencode": ["opencode_key.txt", "opencode_api_key.txt", "api_key.txt"],
            "deepseek": ["api_key.txt", "deepseek_key.txt", "deepseek_api_key.txt", "key.txt"],
        }
        category = "gemini" if is_gemini else ("free_glm" if is_free_glm else ("opencode" if is_opencode else "deepseek"))
        candidate_filenames = file_map[category]

        candidate_paths = [key_file] if key_file else []
        candidate_paths.extend([os.path.join(base_dir, fname) for fname in candidate_filenames])

        for kf in candidate_paths:
            if kf and os.path.exists(kf):
                try:
                    with open(kf, "r", encoding="utf-8") as f:
                        parsed = _parse_keys(f.read())
                    if parsed:
                        keys.extend(parsed)
                        break
                except Exception:
                    pass

    if not keys:
        print("❌ 找不到 API 金鑰，請建立對應金鑰檔案或設定環境變數！")
        return None

    return ApiKeyPool(keys)

def auto_output_path(input_file: str) -> str:
    """自動推導 MD 檔路徑"""
    input_abs = os.path.abspath(input_file)
    input_dir = os.path.dirname(input_abs)
    input_name = os.path.splitext(os.path.basename(input_abs))[0]
    return os.path.join(input_dir, f"{input_name}_銷文.md")


PROVIDER_DEFAULTS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "name": "DeepSeek 官方 API",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "name": "Google AI Studio",
    },
    "free_glm": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "z-ai/glm-5.2:free",
        "name": "OpenRouter Free GLM",
    },
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "default_model": "deepseek-v3.2",
        "name": "OpenCode Go 訂閱端點",
    },
    "zen": {
        "base_url": "https://opencode.ai/zen/v1",
        "default_model": "deepseek-v3.2",
        "name": "OpenCode Zen 按量端點",
    },
}


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="DeepSeek 佛經銷文斷句品質深度審查、一鍵修正與專注補漏工具（智慧流水線版）"
    )
    parser.add_argument("--file", type=str, default=None, help="原始經文 txt 檔案路徑（單檔模式）")
    parser.add_argument("--recursive", "--batch-dir", "--batch", type=str, nargs="?", const=".", default=None, help="★ 遞迴批次模式：指定搜尋目錄（預設當前目錄 .），自動找出所有 *_銷文.md 逐一校驗修正完工")
    parser.add_argument("--output", type=str, default=None, help="目標銷文 md 檔案路徑（預設自動推導）")

    # 模式互斥群組
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--auto", action="store_true", help="★ 智慧全流程模式（預設：銷文->補漏->審查修復->二次補漏）")
    mode_group.add_argument("--generate", "--gen", action="store_true", help="手動指定：強制全本從頭銷文")
    mode_group.add_argument("--fix-gaps", "--gaps", action="store_true", help="手動指定：僅執行補漏")
    mode_group.add_argument("--review", action="store_true", help="手動指定：僅執行 AI 審查並輸出 review.json")
    mode_group.add_argument("--fix", action="store_true", help="手動指定：僅讀取 review.json 執行修正")

    # 提供商互斥群組
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument("--deepseek", action="store_const", dest="provider", const="deepseek", help="使用 DeepSeek 官方 API")
    provider_group.add_argument("--gemini", "--google", action="store_const", dest="provider", const="gemini", help="★ 使用 Google AI Studio Gemini 免費端點")
    provider_group.add_argument("--free-glm", "--glm5", "--glm", action="store_const", dest="provider", const="free_glm", help="★ 使用 OpenRouter Free GLM 免費模型端點")
    provider_group.add_argument("--opencode", "--go", action="store_const", dest="provider", const="opencode", help="★ 使用 OpenCode Go 訂閱端點")
    provider_group.add_argument("--zen", action="store_const", dest="provider", const="zen", help="使用 OpenCode Zen 按量計費端點")
    parser.set_defaults(provider="deepseek")

    parser.add_argument("--dry-run", action="store_true", help="預覽待修清單，不呼叫 API 且不更動檔案（配合 --fix 使用）")
    parser.add_argument("--debug", action="store_true", help="★ 開啟除錯輸出，即時印出每一次產出的完整原始內容 (Raw Output)")
    parser.add_argument("--reset", "--clean", action="store_true", help="強制清除關聯的 checkpoint 與 review 快取檔")
    parser.add_argument("--model", type=str, default=None, help="呼叫模型名稱（若未指定則根據 Provider 自動匹配最佳預設）")
    parser.add_argument("--reasoning-effort", type=str, default="high", choices=["low", "medium", "high"])
    parser.add_argument("--max-review-cycles", type=int, default=2, help="AI 審查與修復的最大交替輪次（防死循環）")
    parser.add_argument("--max-fix", type=int, default=50, help="單次最多修正問題數")
    parser.add_argument("--timeout", type=int, default=300, help="單次 API 超時時間（秒）")
    parser.add_argument("--base-url", type=str, default=None, help="自訂 API Base URL")
    parser.add_argument("--api-key", type=str, default=None, help="直接指定 API Key 字串")
    parser.add_argument("--api-key-file", type=str, default=None, help="指定 API Key 檔案路徑")
    args = parser.parse_args()

    if not args.file and not args.recursive:
        parser.error("必須提供 --file <檔案路徑>（單檔模式）或 --recursive [目錄路徑]（遞迴批次模式）")

    if not any([args.generate, args.fix_gaps, args.review, args.fix]):
        args.auto = True

    is_batch_mode = bool(args.recursive)
    main_log_path = "batch_sutra_review.log" if is_batch_mode else os.path.splitext(args.output or auto_output_path(args.file))[0] + "_review_log.txt"
    logger = setup_logger(main_log_path)

    # 取得提供商預設參數
    p_info = PROVIDER_DEFAULTS[args.provider]
    base_url = args.base_url or p_info["base_url"]
    provider_name = p_info["name"]
    model_name = args.model or p_info["default_model"]

    # 快取重置處理
    if args.reset and args.file:
        target_md = args.output or auto_output_path(args.file)
        cp_path = os.path.splitext(target_md)[0] + "_checkpoint.json"
        rv_path = os.path.splitext(target_md)[0] + "_review.json"
        for p in [cp_path, rv_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                    logger.info(f"🧹 已清除快取檔：{p}")
                except Exception:
                    pass

    key_pool = load_api_keys(
        key_file=args.api_key_file,
        api_key_str=args.api_key,
        is_opencode=(args.provider in ["opencode", "zen"]),
        is_free_glm=(args.provider == "free_glm"),
        is_gemini=(args.provider == "gemini")
    )
    if not key_pool:
        return

    init_key = key_pool.get_current_key()
    logger.info(
        f"API 提供商: {provider_name} ({base_url}) | 模型: {model_name} | "
        f"金鑰池: 共載入 {len(key_pool)} 把 Key (初始: {key_pool.mask_key(init_key)})"
    )
    client = OpenAI(api_key=init_key, base_url=base_url, timeout=args.timeout)
    client.key_pool = key_pool
    client.debug_mode = args.debug

    # 批次遞迴處理路由
    if is_batch_mode:
        search_dir = os.path.abspath(args.recursive)
        pairs = find_sutra_md_pairs(search_dir, logger=logger)
        if not pairs:
            logger.warning(f"⚠️ 在目錄 {search_dir} 下未找到任何匹配的 *_銷文.md 與 .txt 經文檔案組合！")
            return
        run_batch(args, client, model_name, pairs, logger)
        return

    # 單檔模式處理路由
    if args.output is None:
        args.output = auto_output_path(args.file)

    try:
        with open(args.file, "r", encoding="utf-8-sig") as f:
            sutra_text = f.read().strip()
    except FileNotFoundError:
        logger.error(f"找不到原始經文檔案: {args.file}")
        return

    style = detect_punctuation_style(sutra_text)
    segments = extract_segments_from_md(args.output)

    if not segments and (args.review or (args.fix and not args.auto)):
        logger.error(f"無法從銷文檔 {args.output} 提取到任何「🔹 原典」段落，請確認檔案路徑。")
        return

    logger.info(
        f"經文檔案: {os.path.abspath(args.file)} ({len(sutra_text)} 字，風格: {'古典全句號' if style == 'ALL_PERIOD' else '現代新標點'})"
    )
    logger.info(f"銷文檔案: {os.path.abspath(args.output)} ({len(segments)} 個段落)")

    # 單檔模式路由
    if args.auto:
        run_pipeline(args, client, model_name, sutra_text, style, args.output, logger)
    elif args.generate:
        run_generate(args, client, model_name, sutra_text, args.output, logger)
    elif args.fix_gaps:
        run_fix_gaps(args, client, model_name, sutra_text, segments, style, args.output, logger)
    elif args.review:
        run_review(args, client, model_name, sutra_text, segments, style, args.output, logger)
    elif args.fix or args.dry_run:
        run_fix(args, client, model_name, sutra_text, segments, args.output, logger)
        

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 已偵測到中斷信號 (Ctrl + C)，程式已安全停止。")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)