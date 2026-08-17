#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refine.py — 佛經銷文斷句品質深度審查、一鍵修正與專注補漏工具
（雙標點體系全能版 + DeepSeek Prompt Cache 極致優化 + 模組化重構版）

支援雙標點體系：
  1. 現代新標點文本（有逗號、冒號、引號等）
  2. 古典全句號文本（通篇全為句號，句號兼具逗號、分號、停頓功能）

核心特性：
  ★ 遵循「靜態前置、動態後置」鎖定 KV Cache，降低延遲與 API 費用
  ★ 移除非必要的硬性字數限制，改由 AI 依佛學義理與因明文法結構自主決定斷句範圍
  ★ 統一採用「動態指針推進迴圈」，長區間由 AI 自然分段產出，杜絕人工硬切錯誤
  ★ 結合「前文脈絡錨點」與「品質校驗自動重試（防半偈、防跳字、防懸空）」
  ★ 支援斷點續傳（Checkpoint）與 Windows / OneDrive 檔案鎖容錯機制

用法：
  python refine.py --file 1.txt --generate          # ★ 全本銷文模式（將整篇經文視為大漏段，AI自主推進從頭銷文到尾）
  python refine.py --file 1.txt --auto              # ★ 官方 DeepSeek 一鍵全流程（審查+修復+重排）
  python refine.py --file 1.txt --fix-gaps          # ★ 專注補漏模式（快速掃描漏段，AI自主分段補齊並物理歸位）
  python refine.py --file 1.txt --auto --opencode   # ★ 使用 OpenCode Go (opencode.ai) 端點
  python refine.py --file 1.txt --review --opencode # 使用 OpenCode 僅審查並產出 review.json
  python refine.py --file 1.txt --fix --opencode    # 使用 OpenCode 依 review.json 修正
  python refine.py --file 1.txt --fix --dry-run     # 預覽待修正清單（不呼叫 API）
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


# ============================================================
#  一、常數預編譯與古籍文字學映射
# ============================================================
RE_CLEAN_CJK = re.compile(r"[^\w\u4e00-\u9fa5]")
RE_CLEAN_CHAR = re.compile(r"[\w\u4e00-\u9fa5]")
RE_THINK_TAG = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
RE_CODE_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?", re.MULTILINE)
RE_CODE_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$", re.MULTILINE)

# 修正循環映射與無效映射：統一古異體字至現代標準字
VARIANT_CHAR_MAP = str.maketrans({
    "媅": "耽", "怱": "匆", "蘇": "酥", "妒": "妬",
    "睹": "覩", "麤": "粗", "麁": "粗", "併": "并",
    "回": "迴", "嗔": "瞋", "倶": "俱", "缽": "鉢",
    "雲": "云", "凈": "淨", "净": "淨", "註": "注",
    "沈": "沉", "祗": "祇", "袛": "祇", "衹": "祇",
    "只": "祇", "裏": "裡", "墮": "堕", "辨": "辯", "鷄": "雞",
})

DANGLING_PATTERNS = (
    "所以者何", "何以故", "云何", "何者是", "此云何然", "如何得知",
    "為遮為表", "如何得成", "依何得成", "此復二種", "此亦二種",
    "何等為", "何等是", "何等", "云何為", "何以", "若爾"
)


# ============================================================
#  二、日誌與 API 快取指標監控
# ============================================================
def setup_logger(log_file: str) -> logging.Logger:
    """初始化控制台與檔案日誌器"""
    logger = logging.getLogger("sutra_review")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def log_cache_metrics(logger: logging.Logger, resp: Any, action_name: str = "API 調用") -> None:
    """解析並記錄 DeepSeek / OpenAI 規範的 Prompt Cache 命中指標"""
    usage = getattr(resp, "usage", None)
    if not usage:
        return

    prompt_tokens = getattr(usage, "prompt_tokens", 0)
    cached_tokens = 0

    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details and hasattr(prompt_details, "cached_tokens"):
        cached_tokens = prompt_details.cached_tokens or 0
    elif hasattr(usage, "prompt_cache_hit_tokens"):
        cached_tokens = usage.prompt_cache_hit_tokens or 0

    completion_tokens = getattr(usage, "completion_tokens", 0)

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
    text = raw_content.strip()

    # 0. 移除 DeepSeek R1 / 推理模型思維鏈標籤
    text = RE_THINK_TAG.sub("", text).strip()
    if "<think>" in text.lower():
        text = re.sub(r"<think>[\s\S]*", "", text, flags=re.IGNORECASE).strip()

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

    # 4. 移除【下一句預告】及其後所有文字與狀態碼
    text = re.sub(r"(?:[\s#*`>]*【?下一句(?:預告)?】?[\s\S]*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[STATUS:\s*[^\]]*\][\s\S]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"請問是否繼續銷文下一句[？?]?[\s\S]*", "", text)

    # 5. 移除多餘的獨立 Markdown 分隔線
    text = re.sub(r"^\s*---\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def extract_sentence(raw_content: str) -> Optional[str]:
    """精確提取『🔹 原典』中的文字（自動清除外圍代碼塊、引號、Markdown 粗體與孤立標點符號殘肢）"""
    def _clean_s(t: str) -> str:
        t = t.strip()
        for _ in range(3):
            t = re.sub(r"^[`*_\s]+|[`*_\s]+$", "", t).strip()
            t = re.sub(r"^[「『\"'“‘《〈【〔（(]+|[」』\"'”’》〉】〕）)]+$", "", t).strip()
            t = re.sub(r"^[，。！？；、：）\)\]】〕＞》〉\s]+", "", t).strip()
            t = re.sub(r"[（\(\[【〔＜《〈\s]+$", "", t).strip()
        return t

    m = re.search(r"🔹\s*(?:\*\*|#)*\s*原典\s*(?:\*\*)*[：:]\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|$))", raw_content)
    if m and m.group(1).strip():
        return _clean_s(m.group(1))

    block_match = re.search(r"【單句銷文】([\s\S]*?)(?=【詳解】|$)", raw_content)
    search_area = block_match.group(1) if block_match else raw_content

    fallback_patterns = [
        r"(?:\*\*|#)*\s*原典\s*(?:\*\*)*[：:]\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|$))",
        r"【原典】[：:]?\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|$))",
    ]
    for pat in fallback_patterns:
        m = re.search(pat, search_area)
        if m and m.group(1).strip():
            return _clean_s(m.group(1))
    return None


def is_ignorable_gap(gap_text: str) -> bool:
    """判斷是否為純符號空白或經名品題（正文對話與法相一律保留）"""
    clean = RE_CLEAN_CJK.sub("", gap_text)
    if not clean:
        return True

    title_pattern = (
        r"^(?:.*?[經論律])?.*?(?:卷|品|章|分|地)"
        r"(?:第[一二三四五六七八九十百千\d]+)?"
        r"(?:之[一二三四五六七八九十\d初末餘之]+)?$"
    )
    if len(clean) <= 30 and (re.match(title_pattern, clean) or re.match(r"^.*?經卷[一二三四五六七八九十\d]+$", clean)):
        return True
    return False


def validate_output_format(raw_content: str) -> Tuple[bool, List[str]]:
    """驗證輸出格式是否包含全部必備結構（相容 Unicode 符號、Markdown 標題與粗體）"""
    required_sections = [
        ("🔹 原典", r"🔹\s*(?:\*\*|#)*\s*原典|(?:\*\*|#)*\s*原典\s*(?:\*\*)*[：:]|【原典】"),
        ("🔸 釋詞", r"🔸\s*(?:\*\*|#)*\s*釋詞|(?:\*\*|#)*\s*釋詞\s*(?:\*\*)*[：:]|【釋詞】"),
        ("🔸 銷文", r"🔸\s*(?:\*\*|#)*\s*銷文|(?:\*\*|#)*\s*銷文\s*(?:\*\*)*[：:]|【銷文】"),
        ("【詳解】", r"【詳解】|###\s*詳解|(?:\*\*)*\s*詳解\s*(?:\*\*)*[：:]"),
        ("【義理通解】", r"【義理通解】|###\s*義理通解|(?:\*\*)*\s*義理通解\s*(?:\*\*)*[：:]"),
    ]
    missing = [name for name, pat in required_sections if not re.search(pat, raw_content)]
    return (len(missing) == 0), missing


def verify_sentence_quality(
    sentence_text: str,
    remaining_text: str,
    issue_type: str = "",
    problem_desc: str = ""
) -> Tuple[bool, Optional[str], Optional[str]]:
    """統一品質驗證：防腰斬、防半偈、防起點跳字、防懸空、無硬性字數門檻限制"""
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
                return False, f"跳過了前段經文（遺漏 {len(skipped_chars)} 字）", f"前面遺漏了『{skipped_chars[:20]}...』，請嚴格從第一個字開始。"
        else:
            if not is_dup_context:
                return False, "原典不在剩餘經文開頭", "輸出的原典文字與當前經文起點不符，請一字不差照抄。"

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
    is_at_end = len(clean_rem) <= len(clean_s_text) + 2

    # 5. 偈頌音律分析
    clauses_s = [
        RE_CLEAN_CJK.sub("", c)
        for c in re.split(r"[，。！？；、：\s\n　]", sentence_text)
        if RE_CLEAN_CJK.sub("", c)
    ]
    is_5_rhythm = (all(len(c) in [5, 10, 15, 20] for c in clauses_s) and len(clean_s_text) % 5 == 0) if clauses_s else (len(clean_s_text) >= 5 and len(clean_s_text) % 5 == 0)
    is_7_rhythm = (all(len(c) in [7, 14, 21, 28] for c in clauses_s) and len(clean_s_text) % 7 == 0) if clauses_s else (len(clean_s_text) >= 7 and len(clean_s_text) % 7 == 0)
    is_valid_gatha = is_5_rhythm or is_7_rhythm

    # 6. 防懸空設問句與標點殘缺
    if not is_valid_gatha and not is_title and not is_annotation and not is_at_end:
        pure_end = re.sub(r"[\s」』”\"\'\)）］】〕＞》〉]+$", "", sentence_text.strip())
        if pure_end:
            last_char = pure_end[-1]
            valid_puncts = ["。", "！", "？", "：", "；", "◎", "…", "，", "、"]

            clean_end = normalize_text(pure_end)
            is_dangling = any(clean_end.endswith(p) for p in DANGLING_PATTERNS) or bool(
                re.search(r"(?:何等為[一二三四五六七八九十百千\d]+|云何諸法[^。！？]*)$", clean_end)
            )
            if is_dangling and len(clean_rem) > len(clean_s_text) + 5:
                return False, "設問句/過渡句懸空截斷", f"不可在『{clean_end[-8:]}』處中斷！請連同解答一併銷文。"

    return True, None, None


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
        if s_norm and len(s_norm) >= 2:
            valid_sentences.append((idx, s, s_norm))

    sorted_sentences = sorted(valid_sentences, key=lambda x: len(x[2]), reverse=True)

    for orig_idx, s, s_norm in sorted_sentences:
        matches = [m.start() for m in re.finditer(re.escape(s_norm), norm_sutra)]
        if not matches and len(s_norm) >= 4:
            prefix = s_norm[: min(4, len(s_norm))]
            matches = [m.start() for m in re.finditer(re.escape(prefix), norm_sutra)]

        if not matches:
            continue

        best_pos = -1
        max_new_cover = -1
        best_distance = float("inf")
        expected_pos = int((orig_idx / max(1, len(completed_sentences))) * n)

        for pos in matches:
            end_pos = min(pos + len(s_norm), n)
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


def find_missing_gaps(sutra_text: str, completed_sentences: List[str]) -> List[Dict[str, Any]]:
    """基於全域布林覆蓋遮罩的精準漏段掃描"""
    clean_to_raw_map, norm_sutra, covered_mask, sentence_slots = get_sutra_coverage(sutra_text, completed_sentences)
    if not norm_sutra:
        return []

    n = len(norm_sutra)
    gaps = []
    i = 0

    pos_to_seg_idx = {}
    for s_idx, (start_p, end_p) in sentence_slots.items():
        for p in range(start_p, end_p):
            pos_to_seg_idx[p] = s_idx

    while i < n:
        if not covered_mask[i]:
            gap_start_clean = i
            while i < n and not covered_mask[i]:
                i += 1
            gap_end_clean = i

            gap_start_raw = clean_to_raw_map[gap_start_clean]
            while gap_start_raw > 0 and sutra_text[gap_start_raw - 1] in "「『“‘（([【〔<《〈":
                gap_start_raw -= 1

            if gap_end_clean < len(clean_to_raw_map):
                gap_end_raw = clean_to_raw_map[gap_end_clean]
                while gap_end_raw > gap_start_raw and sutra_text[gap_end_raw - 1] in "「『“‘（([【〔<《〈":
                    gap_end_raw -= 1
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

                gaps.append({
                    "prev_idx": prev_idx,
                    "gap_text": gap_raw,
                    "position": pos_type
                })
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

    raw_start = clean_to_raw_map[start_clean_pos] if start_clean_pos < len(clean_to_raw_map) else 0
    while raw_start > 0 and sutra_text[raw_start - 1] in open_brackets:
        raw_start -= 1

    if end_clean_pos >= len(clean_to_raw_map):
        raw_end = len(sutra_text)
    else:
        raw_end = clean_to_raw_map[end_clean_pos - 1] + 1
        while raw_end < len(sutra_text) and sutra_text[raw_end] in trailing_chars and sutra_text[raw_end] not in open_brackets:
            raw_end += 1

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
def safe_write_file(filepath: str, content: str) -> None:
    """Windows / OneDrive 檔案鎖容錯強化版原子寫入"""
    if os.path.exists(filepath):
        try:
            shutil.copy2(filepath, filepath + ".bak")
        except Exception:
            pass

    temp_path = filepath + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return

    replaced = False
    for _ in range(5):
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
            time.sleep(0.3)

    if not replaced:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


def extract_segments_from_md(filepath: str) -> List[str]:
    """從 MD 檔案中依序提取出所有原典段落"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    raw_matches = re.findall(
        r"🔹\s*(?:\*\*|#)*\s*原典\s*(?:\*\*)*[：:]\s*([\s\S]*?)(?=(?:\n\s*🔸|\n\s*【|\n---|$))",
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
    """將 MD 拆解為大標題與段落獨立區塊，自動黏合孤立殘片"""
    if not os.path.exists(filepath):
        return "", []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    header = ""
    m = re.match(r"^(#\s+[^\n]+\n+)", content)
    if m:
        header = m.group(1)
        content = content[len(m.group(1)):]

    raw_blocks = [
        s.strip()
        for s in re.split(
            r"(?:\n\s*---\s*\n|(?<=\n)(?=(?:【當前經文進度】|【單句銷文】|🔹\s*(?:\*\*)*原典)))",
            content
        )
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
            sections.append(b)
        else:
            if not extract_sentence(b) and re.match(r"^(?:【詳解】|【義理通解】|【釋詞】|🔸|（|\()", b):
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

    raw_sections = [
        s.strip()
        for s in re.split(
            r"(?:\n\s*---\s*\n|(?<=\n)(?=(?:【當前經文進度】|【單句銷文】|🔹\s*(?:\*\*)*原典)))",
            content_body
        )
        if s.strip()
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
            assigned_sections.append({
                "pos": 9999999,
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
        sec_hash = hash(re.sub(r"\s+", "", item["sec"]))
        norm_s = item.get("norm_s", "")
        if not norm_s:
            s_extracted = extract_sentence(item.get("sec", ""))
            norm_s = normalize_text(s_extracted) if s_extracted else ""
        pos = item["pos"]
        count_in_sutra = (norm_sutra.count(norm_s) if len(norm_s) < 15 else norm_sutra.count(norm_s[:15])) if norm_s else 0

        if count_in_sutra <= 1 and pos != 9999999:
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
    """將修正結果寫回 MD 檔，並進行經文位置物理校準重排"""
    header, sections = parse_md_sections(filepath)
    corrections.sort(key=lambda x: x[1][0], reverse=True)

    for new_content, merge_idx in corrections:
        if not new_content:
            continue

        target_indices_in_sections = []
        for orig_i in merge_idx:
            if 0 <= orig_i < len(segments_snapshot):
                target_seg = segments_snapshot[orig_i]
                norm_target = normalize_text(target_seg)

                search_window = sorted(
                    range(max(0, orig_i - 5), min(len(sections), orig_i + 6)),
                    key=lambda idx: abs(idx - orig_i)
                )
                matched = False
                for sec_idx in search_window:
                    sec_orig_sent = extract_sentence(sections[sec_idx])
                    sec_orig_norm = normalize_text(sec_orig_sent) if sec_orig_sent else ""
                    if norm_target and sec_orig_norm and (norm_target in sec_orig_norm or sec_orig_norm in norm_target):
                        if sec_idx not in target_indices_in_sections:
                            target_indices_in_sections.append(sec_idx)
                        matched = True
                        break

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
                    for b in re.split(r"\n\s*---\s*\n", new_content)
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
2. 【自然切分與無最低字數限制】：
   - ★【無最低字數限制】：AI 斷句再短皆完全接受！即便當前單元只有極短的數個字（如精練問答「王言：不也。」、法相標題、短句），只要語意自足，皆完全合法，無需強行湊字數。
   - ★【重點在重跑時自然切分】：若待銷文經文較長，請【切分出當前第一個語意自足的獨立單元】（如散文 1 個完整句意段落；偈頌 1 整偈或半偈；對話 1 輪問答）進行銷文。未處理的後續經文系統會自動在下一輪繼續推進。
   - 【古典全句號與因明論典】：古典句號兼具逗號、分號功能，請保持「先縱後奪」、「攻防破斥」與「設問+解答」的語意完整，避免懸空截斷。

【銷文與解義原則】
1. 極盡詳盡的銷解：依傳統講經「消文釋義」的方式，鋸細靡遺地拆解該句經文的文言句法與字面意義，落實每一個字、詞的作用，不可含糊帶過。
2. 義理通解：在考證與詳解之後，需將此句經文的核心教理進行全面性的統攝與貫通，並可配合現代化比喻助解。

【輸出格式】 (請嚴格按照以下結構輸出，不可遺漏任何段落)

【當前經文進度】：(簡述目前正在處理的是指定範圍內的哪一句)

【單句銷文】：
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
    """封裝串流 LLM 呼叫，包含 extra_body 降級與快取指標記錄"""
    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 384000,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_effort:
        create_kwargs["extra_body"] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort,
        }

    try:
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as api_err:
        # 若因 extra_body 不相容失敗，自動降級重試
        if "extra_body" in create_kwargs:
            create_kwargs.pop("extra_body", None)
            resp = client.chat.completions.create(**create_kwargs)
        else:
            raise api_err

    content_parts = []
    last_chunk = None
    for chunk in resp:
        last_chunk = chunk
        if chunk.choices and chunk.choices[0].delta.content:
            content_parts.append(chunk.choices[0].delta.content)

    if last_chunk:
        log_cache_metrics(logger, last_chunk, action_name=action_name)

    return "".join(content_parts)


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

            # 安全防護：若剩餘開頭殘留 <=2 個純虛詞/標點殘肢，自動吸納清除避免卡在單個字
            rem_clean = normalize_text(remaining_text)
            if 0 < len(rem_clean) <= 2 and any(rem_clean.endswith(p) for p in ["者", "也", "耳", "矣", "焉"]):
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
    - 支援成功即時安插與物理重排
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
            "\n4. 【重複內容特別指引】：若前文已包含部分重複經文，請僅針對指定的剩餘經文進行銷文；"
            "若本段經文已完全重複，請直接輸出 <!-- DELETE -->。\n"
        )

    is_gap_mode = ("經文漏段補齊" in issue_type or "補漏" in issue_type or "全本經文銷文" in issue_type)
    guideline_title = "【補漏指引與核心規範】" if is_gap_mode else "【修復通用指引與核心規範】"
    first_rule = (
        "1. 這是一段先前遺漏的經文，請切分出當前第一個語意自足的獨立單元進行銷文，並符合所有銷文結構規範。"
        if is_gap_mode
        else "1. 請切出當前第一個語意自足的獨立單元進行銷文，並符合所有銷文結構規範。"
    )

    while remaining_text and len(normalize_text(remaining_text)) > 0 and loop_guard < max_loops:
        loop_guard += 1
        success_block = None
        extracted_sent = None

        for retry in range(3):
            problem_section = f"【本處病灶與修正建議】：\n{problem_desc}\n\n" if (problem_desc and not is_gap_mode) else ""
            user_msg = (
                f"【經典全本文脈背景】：\n{sutra_text}\n\n"
                f"{guideline_title}：\n"
                f"{first_rule}\n"
                f"2. 嚴禁跳字、漏字，原典必須完全忠於經文，字字精確，不得包含省略號。\n"
                f"3. 保持義理完整，按規範格式完整輸出【當前經文進度】、【單句銷文】、【詳解】、【義理通解】。{dup_guide}\n"
                f"【前文脈絡錨點】：\n{curr_prev_sentence if curr_prev_sentence else '（經文起始段落）'}\n\n"
                f"{problem_section}"
                f"【🚨 當前待銷文剩餘經文（請嚴格從第一個字開始）】：\n{remaining_text}"
            )

            try:
                raw_reply = stream_completion(
                    client=client,
                    model=model,
                    system_prompt=FIX_SYSTEM,
                    user_prompt=user_msg,
                    reasoning_effort=reasoning_effort,
                    logger=logger,
                    action_name=f"{'補漏銷文' if is_gap_mode else '修復銷文'} (輪次 {loop_guard}, 重試 {retry})"
                )

                cleaned_reply = clean_markdown_content(raw_reply)
                extracted_sent = extract_sentence(cleaned_reply)

                if not extracted_sent:
                    if (
                        "重複" in issue_type
                        and len(normalize_text(cleaned_reply)) < 50
                        and not re.search(r"🔸|🔹|【", cleaned_reply)
                    ):
                        success_block = "<!-- DELETE -->"
                        remaining_text = ""
                        break
                    logger.warning(f"    ⚠️ [重試 {retry+1}/3] 無法從輸出中解析出『🔹 原典』")
                    continue

                if "重複" in issue_type and (
                    extracted_sent.strip() in ["無", "（無）", "(無)", "none", "null", ""]
                    or len(normalize_text(extracted_sent)) == 0
                ):
                    success_block = "<!-- DELETE -->"
                    remaining_text = ""
                    break

                valid, err_type, err_advice = verify_sentence_quality(
                    extracted_sent,
                    remaining_text,
                    issue_type,
                    problem_desc=problem_desc
                )
                if not valid:
                    logger.warning(f"    ⚠️ [重試 {retry+1}/3] 品質校驗未通過 ({err_type})：{err_advice}")
                    continue

                fmt_ok, missing = validate_output_format(cleaned_reply)
                if not fmt_ok:
                    if (
                        "重複" in issue_type
                        and (
                            not extracted_sent
                            or len(normalize_text(extracted_sent)) == 0
                            or extracted_sent.strip() in ["無", "（無）", "(無)", "none", "null", ""]
                        )
                    ):
                        success_block = "<!-- DELETE -->"
                        remaining_text = ""
                        break
                    logger.warning(f"    ⚠️ [重試 {retry+1}/3] 格式缺少必要欄位：{missing}")
                    continue

                success_block = cleaned_reply
                break

            except KeyboardInterrupt:
                logger.warning("\n🛑 使用者中斷了修復流程 (Ctrl+C)")
                raise
            except Exception as e:
                err_msg = str(e)
                if any(kw in err_msg for kw in ["Insufficient balance", "CreditsError", "AuthenticationError", "invalid_api_key", "401"]):
                    logger.error(f"❌ API 金鑰無效或帳號餘額不足 ({err_msg})！請確認金鑰或儲值後重試。")
                    return None, remaining_text
                backoff_time = (retry + 1) * 3
                logger.error(
                    f"    ❌ API 呼叫失敗 ({e})，當前待處理經文剩餘 {len(normalize_text(remaining_text))} 字，"
                    f"等待 {backoff_time} 秒後進行第 {retry+1}/3 次重試..."
                )
                time.sleep(backoff_time)

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

            # 推進剩餘文字指針（使用純函數）
            remaining_text = advance_text_pointer(remaining_text, extracted_sent)

            if remaining_text == "":
                pass
            elif len(normalize_text(remaining_text)) == 0:
                remaining_text = ""

            logger.info(
                f"    ✅ 子單元銷文成功：『{extracted_sent[:25]}...』"
                f"(剩餘 {len(normalize_text(remaining_text))} 字)"
            )
            if len(normalize_text(remaining_text)) > 0:
                logger.info(f"       -> 剩餘待處理經文長度: {len(normalize_text(remaining_text))} 字")

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
    issue: Dict[str, Any],
    segments: List[str],
    logger: logging.Logger,
    reasoning_effort: str = "high",
    partial_state: Optional[Dict[str, Any]] = None,
    on_step_done: Optional[Callable[[List[int], Dict[str, Any]], None]] = None,
    output_path: Optional[str] = None
) -> Tuple[Optional[str], List[int]]:
    """針對特定審查問題進行段落合併重寫"""
    merge_idx = sorted(list(set(issue.get("merge_indices", [issue["index"]]))))
    valid_merge_idx = [i for i in merge_idx if 0 <= i < len(segments)]
    merge_segs = [segments[i] for i in valid_merge_idx]

    first_idx = valid_merge_idx[0] if valid_merge_idx else 0
    last_idx = valid_merge_idx[-1] if valid_merge_idx else 0
    prev_sentence = segments[first_idx - 1] if first_idx > 0 and (first_idx - 1) < len(segments) else ""

    issue_type_str = issue.get("type", "")
    is_gap_fix = "經文漏段補齊" in issue_type_str
    is_head_gap = (is_gap_fix and (issue.get("position") == "head" or first_idx == 0))
    is_tail_gap = (is_gap_fix and (issue.get("position") == "tail" or last_idx == len(segments) - 1))
    problem_desc = issue.get("problem", "")

    # ★ 孤立標點/括號快速修復直通
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
        gap_len = len(normalize_text(issue.get("gap_text", "")))
        expected_raw_len = sum(len(normalize_text(s)) for s in merge_segs) + gap_len
        if len(normalize_text(combined_raw)) > max(expected_raw_len * 2 + 100, expected_raw_len + 150) and not (is_head_gap or is_tail_gap or is_gap_fix):
            logger.warning(
                f"  ⚠️ 檢測到切片長度異常膨脹 ({len(combined_raw)} 字 vs 預期 {expected_raw_len} 字)，"
                f"安全回退至段落拼合"
            )
            combined_raw = "".join(merge_segs)
    else:
        combined_raw = issue.get("gap_text", "")

    if not combined_raw:
        combined_raw = "".join(merge_segs)

    if not combined_raw.strip():
        logger.warning(f"  ⚠️ 修正目標文本為空，跳過段落 {valid_merge_idx}")
        return None, valid_merge_idx

    # 完全重複段落快速直通
    if "重複" in issue_type_str and not is_gap_fix and not issue.get("gap_text"):
        is_full_delete = any(kw in problem_desc for kw in ["應刪除", "建議刪除", "應予刪除", "完全重複", "重複應刪除", "請刪除"]) and not any(
            kw in problem_desc for kw in ["保留", "部分", "重新切分", "拆分", "前綴", "首句", "末句", "首二句", "移回", "補齊", "補全"]
        )
        if is_full_delete:
            logger.info(f"  🗑️ 判定為完全重複段落，直接標記刪除：段落 {valid_merge_idx}")
            if on_step_done and callable(on_step_done):
                on_step_done(valid_merge_idx, "<!-- DELETE -->")
            return "<!-- DELETE -->", valid_merge_idx

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
def pre_check(sutra_text: str, segments: List[str], md_sections: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """程式化純物理硬傷預檢（碎首/斷尾 + 非整偈 + 格式殘缺 + 全局漏段檢測）"""
    style = detect_punctuation_style(sutra_text)
    issues = []
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
                    issues.append({
                        "index": i,
                        "type": "重複內容",
                        "problem": f"本段經文與第 [{dup_orig_idx}] 段完全重複（原典僅出現 1 次），應刪除重複",
                        "merge_indices": [i],
                    })
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
                    issues.append({
                        "index": i,
                        "type": "重複內容",
                        "problem": f"本段首句「{dup_str[:10]}...」與前段實質重複，應刪除重複部分，保留後續經文",
                        "merge_indices": [i],
                    })

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
                    issues.append({
                        "index": i,
                        "type": "銷文格式殘缺",
                        "problem": f"本段格式不完整，缺少必要區塊：{missing}，應重新銷文補全",
                        "merge_indices": [i],
                    })
            continue

        # 1. 現代標點腰斬
        pure_end = re.sub(r"[\s」』”\"\'\)）］】〕＞》〉]+$", "", raw_seg)
        if pure_end and pure_end[-1] in ["，", "、", "—", "-"] and not is_at_end:
            issues.append({
                "index": i,
                "type": "標點腰斬未完",
                "problem": f"段落結尾停在未完標點「{pure_end[-1]}」，句子未說完，應與下段合併",
                "merge_indices": [i, i + 1] if i + 1 < total else [i],
            })
            continue

        # 2. 開頭標點殘肢
        if i > 0 and raw_seg.startswith(("，", "、", "；", "。", "！", "？", "：")):
            prev_seg = segments[i - 1].strip()
            prev_pure_end = re.sub(r"[\s」』”\"\'\)）］】〕＞》〉]+$", "", prev_seg)
            if prev_pure_end and prev_pure_end[-1] in ["，", "、", "：", "；", "—", "-"]:
                issues.append({
                    "index": i - 1,
                    "type": "開頭標點殘肢",
                    "problem": f"段落開頭為標點「{raw_seg[0]}」且前段未完，應與前段合併",
                    "merge_indices": [i - 1, i],
                })
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
                    issues.append({
                        "index": i - 1,
                        "type": "字詞開頭碎首腰斬",
                        "problem": f"本段開頭在詞中被截斷（前字『{char_before}』無標點分隔），應與前段合併",
                        "merge_indices": [i - 1, i],
                    })
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
                            issues.append({
                                "index": i,
                                "type": "字詞結尾斷尾腰斬",
                                "problem": f"本段結尾在詞中截斷且緊接後文漢字『{sutra_text[raw_next_idx]}』，應與下段合併",
                                "merge_indices": [i, i + 1],
                            })
                            continue

        # 6. 格式殘缺檢測
        if md_sections and i < len(md_sections):
            valid, missing = validate_output_format(md_sections[i])
            if not valid:
                issues.append({
                    "index": i,
                    "type": "銷文格式殘缺",
                    "problem": f"本段格式不完整，缺少必要區塊：{missing}，應重新銷文補全",
                    "merge_indices": [i],
                })
                continue

    # 7. 全局物理級漏段/跳字精確檢測
    missing_gaps = find_missing_gaps(sutra_text, segments)
    for g in missing_gaps:
        p_idx = g["prev_idx"]
        gap_content = g["gap_text"]
        pos = g["position"]

        if pos == "head" or p_idx < 0:
            target_indices = [0]
            desc = f"經文開頭遺漏了 {len(normalize_text(gap_content))} 字未銷文（『{gap_content[:20]}...』），應合併首段重新切片"
        elif pos == "tail":
            target_indices = [total - 1] if total > 0 else [0]
            desc = f"經文結尾遺漏了 {len(normalize_text(gap_content))} 字未銷文（『{gap_content[:20]}...』），應補齊末尾段落"
        else:
            target_indices = [p_idx, p_idx + 1] if p_idx + 1 < total else [p_idx]
            desc = f"段落 [{p_idx}] 與後文夾縫間遺漏了 {len(normalize_text(gap_content))} 字（『{gap_content[:20]}...』），應由原始經文重新切片補齊"

        issues.append({
            "index": target_indices[0] if (target_indices and target_indices[0] >= 0) else 0,
            "type": "經文漏段補齊",
            "problem": desc,
            "merge_indices": [idx for idx in target_indices if idx >= 0] or [0],
            "gap_text": gap_content,
            "position": pos,
        })

    return issues


REVIEW_SYSTEM = """你是精通三藏文法、因明論理、佛經科判與講經實務的資深佛學審查導師。

【核心指導哲學：講經科判與獨立法義思維】
佛經銷文解義的目的，是將經文切分為「最適宜開示與深入剖析的法義單元」。
我們追求的是【微觀句意自足、宏觀利於深解】，審查需兼顧【防割裂（合併）】與【防臃腫（拆分）】雙向平衡。
★【防崩潰鐵律】：寧可保留微觀深入的法相單元，也【絕對嚴禁滾雪球式強行合併大段】，嚴防造成銷文臃腫與模型輸出超時崩潰！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【🚨 終極判定心法：嚴格區分「語法殘缺（必合）」與「條列分段（放行）」】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
審查每一處段落時，請嚴格套用【三秒講經檢驗法】：
問自己：「若將此段單獨印在講義上作一次開示，在文法上它是一句『沒講完的殘句（缺主語/缺受詞/引語懸空）』，還是『已具備獨立訴求或法義焦點的完整法相/句子』？」

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🟥 一、何時【必須通報合併】？（修復實質語法割裂 -> merge_indices 填 [相鄰段落]）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 【句法成分硬性斷裂】：
   - 專有名詞跨段腰斬（如前段末「阿彌陀」，後段首「佛」）。
   - 純說法引導詞/呼語孤立成段（如前段僅有「佛告觀自在菩薩曰：『善男子！』」，正文全落在下一段）。
   - 純冒號/清單總標句單獨成段（如前段僅有「佛告...曰：『善男子！略有八種：』」而無任何子項內容）。
2. 【條件/因果前綴懸空】：
   - 僅有假設子句或因由前綴（如「若彼所生」、「以是因緣」），無主句、無結論、無破斥，單獨存在無法成義。
3. 【單一法相名詞被腰斬成半截】：
   - 在同一句法單元內部被生硬切斷（如前段末「此名精明」，後段首「流溢前境」）。
4. 【半偈殘篇】：
   - 五言/七言韻文在非整偈處中斷（單段僅 5/7 字且句意未完）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✂️ 二、何時【必須標記拆分】？（修復多層次堆疊 -> merge_indices 僅填 [單一段落編號]）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
凡單一段落過長（散文 > 110~140 字，或包含多個明顯可獨立的法義層次），必須標記拆分：
1. 【問答角色混雜（發起問＋佛答塞在同段）】：
   - 前半段為請法者之問（如「如來先說……開我蒙悋」），後半段直接轉為佛陀之答（如「佛告阿難……」），【應在『佛告…』處標記拆分】。
2. 【多重論理層次／喻法過度堆疊】：
   - 單段同時包含「設問」「譬喻展開」「法合應用」「總結定判」多個完整階段，導致銷文臃腫，【應按論理階段拆分】。
3. 【多重法相／多根並列堆疊】：
   - 將兩個或多個原本獨立對稱的法相結構（如同時包含舌根生起與身根生起、或多個不同大種之論述）合併在單一段落，【應按法相門類拆分】。
4. 【偈頌連續堆疊過長】：
   - 偈頌連續堆疊超過 2 整偈（五言 >40 字、七言 >56 字），【應拆分為 1~2 偈之獨立單元】。
★【拆分標記規範】：`type` 填 `單段過長需拆分` 或 `偈頌過長需拆分`，`merge_indices` 【必須嚴格只填單一索引 [段號]】（如 `[49]`）。
★【重複內容標記規範】：
   - 若整段與前後文完全重複：`type` 填 `重複內容`，`problem` 明確寫「與第X段完全重複，應刪除」，`merge_indices` 填 `[該段編號]`。
   - 若僅部分子句重複：`problem` 明確寫「首X句與前段重複，保留『...』」或「首X句重複，應自『...』起重新銷文」，`merge_indices` 填 `[該段編號]`。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🟩 三、何時【堅決放行免審（核心豁免原則）】？（重要！絕對嚴禁通報合併！）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. ★【對稱列舉之獨立法相條目（一者...二者...三者...）—— 堅決放行免審！】：
   - 凡經典中對稱排比、條列之法相項目（例如《解深密經》二十二種愚癡之「一者、執著補特伽羅及法愚癡」「二者、惡趣雜染愚癡；及彼麁重為所對治」、三種自性、三無自性性、五明處、七真如、四種威德、六種相違事、六種異熟果等）。
   - 【豁免判定】：只要每一子條目各自承載獨立的法相定義，即使末尾標點為分號（；）或逗號，【皆屬極佳之微觀剖析單元，絕對嚴禁將一者、二者、三者強行合併成超大段落】！
2. ★【法相品類差別（布施三品、持戒三品、忍三品、精進三品、靜慮三品、慧三品等）—— 堅決放行免審！】：
   - 例如「施三種者：一者、法施；」「二者、財施；」「三者、無畏施。」
   - 【豁免判定】：每一品類皆有極其深厚的獨立法義剖析空間，各自成段最利於精準銷解，【一律視為合法獨立單元，嚴禁合併】！
3. ★【階位進程之「障礙/病（未滿）」與「對治/成滿（藥）」成對結構 —— 堅決放行免審！】：
   - 例如十一種分之「而未能於微細毀犯誤現行中正知而行，由是因緣，於此分中猶未圓滿。」與「為令此分得圓滿故，精勤修習便能證得，彼諸菩薩由是因緣，此分圓滿。」
   - 【豁免判定】：一段專論「所斷障礙與過患（病）」，下一段專論「能治行門與功德圓滿（藥）」，法義焦點涇渭分明，【堅決放行獨立成段，嚴禁多段連鎖合併】！
4. ★【因明分破與排比料簡原則】：
   - 破斥論證中的各個分支假定（「若一者……；」、「若異者……？」），每個分支都是獨立的攻防層次，各自成段最利於深入發揮，【一律視為合法單元】。
5. ★【起承轉合之自足單元（因果命題、多重譬喻、法數開展）】：
   - 以「是故應知…」、「又如…」、「一者…二者…」開頭的句子，只要內部句意完整、能獨立成義，皆屬標準的科判分段，【嚴禁視為連詞懸空】。
6. ★【精簡問答與印可語】：
   - 簡短的問答輪次、述成印可（「不也，世尊。」、「如是，如是。」），文意自足即屬合法，【嚴禁因字數少而強行與後文全部條目合併】。
7. ★【相鄰段落之定型過渡句/複誦句輕微重疊 —— 堅決放行免審！】：
   - 相鄰段落開頭帶有數個字至十餘字的承上轉折語（如「說是語已」、「由此道理當知…」、「若於爾時…」），只要本段後半具備獨立的主體經文與完整銷文，【一律放行，嚴禁通報重複修復】；僅有全段 100% 純重複且無新法義時才標記刪除。

輸出格式：嚴格輸出 JSON 陣列，不要任何 markdown 標籤或無關廢話。
[{"index": 段落編號, "type": "斷句邊界錯位|字詞腰斬|半偈殘篇|條件句腰斬|單段過長需拆分|偈頌過長需拆分|重複內容", "problem": "簡述具體語法錯置原因與正確邊界切分建議", "merge_indices": [要處理的段落編號, 如邊界錯位填 [42, 43]，單段拆分填 [29]]}]
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
        "【文本風格】：古典全句號文本（句號包含正常句讀與停頓，只要單元語意可獨立銷文即屬合格，切勿過度合併）。"
        if style == "ALL_PERIOD"
        else "【文本風格】：現代新標點文本。"
    )
    user_msg = (
        f"【完整原始經文】：\n{sutra_text}\n\n"
        f"{style_hint}\n\n"
        f"【已完成銷文之原典段落列表】（共 {len(segments)} 段）：\n{seg_list}"
    )

    logger.info(f"AI 審查：正在進行全局語意流暢度與因明攻防邏輯校驗（共 {len(segments)} 段）...")

    for retry in range(3):
        try:
            text = stream_completion(
                client=client,
                model=model,
                system_prompt=REVIEW_SYSTEM,
                user_prompt=user_msg,
                reasoning_effort=reasoning_effort,
                logger=logger,
                action_name="AI 斷句審查"
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
                    parsed = json.loads(clean_m)
                else:
                    parsed = []

            if isinstance(parsed, dict):
                for k in ["issues", "data", "result", "problems"]:
                    if k in parsed and isinstance(parsed[k], list):
                        issues = parsed[k]
                        break
                else:
                    issues = []
            elif isinstance(parsed, list):
                issues = parsed
            else:
                issues = []

            valid_issues = []
            max_idx = len(segments) - 1
            for iss in issues:
                raw_merge = iss.get("merge_indices", [iss.get("index", -1)])
                filtered_merge = [idx for idx in raw_merge if isinstance(idx, int) and 0 <= idx <= max_idx]
                if filtered_merge:
                    iss["index"] = filtered_merge[0]
                    iss["merge_indices"] = sorted(list(set(filtered_merge)))
                    valid_issues.append(iss)

            logger.info(
                f"AI 審查完成：檢出 {len(valid_issues)} 處有效邏輯割裂或斷句不當之處"
                f"（已過濾 {len(issues) - len(valid_issues)} 處越界幻覺）"
            )
            return valid_issues

        except KeyboardInterrupt:
            logger.warning("\n🛑 使用者中斷了 AI 審查流程 (Ctrl+C)")
            raise
        except Exception as e:
            err_msg = str(e)
            if any(kw in err_msg for kw in ["Insufficient balance", "CreditsError", "AuthenticationError", "invalid_api_key", "401"]):
                logger.error("❌ API 金鑰無效或帳號餘額不足！請至平台儲值或更換 API Key。")
                return None
            backoff = (retry + 1) * 3
            logger.error(f"  ❌ AI 審查呼叫失敗 ({e})，等待 {backoff} 秒後進行第 {retry+1}/3 次重試...")
            time.sleep(backoff)

    logger.error("❌ AI 審查重試 3 次皆失敗，放棄本次審查。")
    return None


def merge_overlapping_issues(issues: List[Dict[str, Any]], max_valid_len: Optional[int] = None) -> List[Dict[str, Any]]:
    """自動合併重疊的修復區間（確保區間連續無斷層）"""
    if not issues:
        return []

    clean_issues = []
    for x in issues:
        m_idx = [
            i
            for i in x.get("merge_indices", [x.get("index", 0)])
            if (max_valid_len is None or (isinstance(i, int) and 0 <= i < max_valid_len))
        ]
        if m_idx:
            item = dict(x)
            min_i, max_i = min(m_idx), max(m_idx)
            item["merge_indices"] = list(range(min_i, max_i + 1))
            item["index"] = min_i
            clean_issues.append(item)

    if not clean_issues:
        return []

    changed = True
    current_list = clean_issues
    while changed:
        changed = False
        merged = []
        visited = set()
        for i in range(len(current_list)):
            if i in visited:
                continue
            cur = dict(current_list[i])
            cur_set = set(cur["merge_indices"])
            for j in range(i + 1, len(current_list)):
                if j in visited:
                    continue
                other_set = set(current_list[j]["merge_indices"])
                if cur_set & other_set:
                    min_span = min(min(cur_set), min(other_set))
                    max_span = max(max(cur_set), max(other_set))
                    span_len = max_span - min_span + 1
                    is_subset = other_set.issubset(cur_set) or cur_set.issubset(other_set)

                    if is_subset or span_len <= 8:
                        cur["merge_indices"] = list(range(min_span, max_span + 1))
                        cur["index"] = min_span
                        cur_set = set(cur["merge_indices"])
                        cur["type"] = f"{cur.get('type', '')}+{current_list[j].get('type', '')}"
                        cur["problem"] = f"{cur.get('problem', '')}；{current_list[j].get('problem', '')}"
                        if not cur.get("gap_text") and current_list[j].get("gap_text"):
                            cur["gap_text"] = current_list[j]["gap_text"]
                        if not cur.get("position") and current_list[j].get("position"):
                            cur["position"] = current_list[j]["position"]
                        visited.add(j)
                        changed = True
                    else:
                        non_overlap = [idx for idx in other_set if idx not in cur_set]
                        if non_overlap:
                            min_no, max_no = min(non_overlap), max(non_overlap)
                            current_list[j]["merge_indices"] = list(range(min_no, max_no + 1))
                            current_list[j]["index"] = min_no
                        else:
                            visited.add(j)
                        changed = True
            merged.append(cur)
        current_list = merged

    current_list.sort(key=lambda x: x["merge_indices"][0])
    return current_list


# ============================================================
#  八、斷點續傳 (Checkpoint) 輔助
# ============================================================
def load_checkpoint(checkpoint_path: str) -> Dict[Tuple[int, ...], Any]:
    """讀取中斷前已完成的銷文快取"""
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                tuple(item["merge_indices"]): item["new_content"]
                for item in data
                if "merge_indices" in item and "new_content" in item
            }
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
    """★ 統一經文推進引擎（數學本質：從零銷文 ≡ 遺漏率 100% 的補漏）"""
    logger.info("=" * 65)
    logger.info(f"🚀 啟動 {mode_title} 模式：{output_path}")
    logger.info("=" * 65)

    # 預先校準已存在的 MD 結構
    if os.path.exists(output_path):
        header, sections = parse_md_sections(output_path)
        if sections:
            cur_body = (header + "\n\n---\n\n" if header else "") + "\n\n---\n\n".join(sections)
            reordered = reorder_markdown_by_sutra(cur_body, sutra_text)
            safe_write_file(output_path, reordered)

    existing_segments = extract_segments_from_md(output_path) if os.path.exists(output_path) else []
    missing_gaps = find_missing_gaps(sutra_text, existing_segments)

    if not missing_gaps:
        logger.info("✅ 經文覆蓋率已達 100%，全文皆已銷文完畢，無須重複生成！")
        return

    total_gap_chars = sum(len(normalize_text(g["gap_text"])) for g in missing_gaps)
    logger.info(f"📖 待處理經文共 {len(missing_gaps)} 個區間（總計 {total_gap_chars} 字），開始逐段推進...")
    total_added_blocks = 0

    # 讀取或建立初始 MD 結構，避免每個區塊都重新讀寫檔案
    if os.path.exists(output_path):
        header, cur_sections = parse_md_sections(output_path)
    else:
        cur_sections = []
        base_name = os.path.splitext(os.path.basename(output_path))[0].replace("_銷文", "")
        header = f"# 佛經銷文：{base_name}\n\n"

    for gap_i, g in enumerate(missing_gaps, 1):
        gap_raw = g["gap_text"].strip()
        p_idx = g["prev_idx"]
        prev_sentence = existing_segments[p_idx] if (existing_segments and 0 <= p_idx < len(existing_segments)) else ""

        logger.info(f"\n[{gap_i}/{len(missing_gaps)}] 正在推進銷文（長度 {len(normalize_text(gap_raw))} 字）：『{gap_raw[:30]}...』")
        initial_section_count = len(cur_sections)

        def block_success_handler(block_content: str, sent: str):
            nonlocal total_added_blocks, prev_sentence
            total_added_blocks += 1
            prev_sentence = sent
            cur_sections.append(block_content)

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

        # 單一 Gap 完成或中斷時，統一寫入一次檔案，避免每產出 1 個 Block 就全檔重排
        if len(cur_sections) > initial_section_count:
            combined_body = (header.strip() + "\n\n---\n\n" if header.strip() else "") + "\n\n---\n\n".join(cur_sections)
            reordered_md = reorder_markdown_by_sutra(combined_body, sutra_text)
            safe_write_file(output_path, reordered_md)

        if not blocks or (rem_after and len(normalize_text(rem_after)) > 2):
            logger.error(f"  ❌ 經文推進於『{gap_raw[:20]}...』處中斷。")
            break

        # 即時提取最新段落快照，確保下一段漏段的「前文脈絡錨點」無縫銜接
        existing_segments = extract_segments_from_md(output_path)

    # 計算最終覆蓋率指標
    latest_segments = extract_segments_from_md(output_path)
    final_gaps = find_missing_gaps(sutra_text, latest_segments)
    clean_sutra_len = len(normalize_text(sutra_text))
    total_gaps_chars = sum(len(normalize_text(g["gap_text"])) for g in final_gaps)
    covered_chars = max(0, clean_sutra_len - total_gaps_chars)
    coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

    logger.info("\n" + "=" * 65)
    logger.info(f"🎉 {mode_title} 執行完畢！本次共產出/補齊 {total_added_blocks} 個段落。")
    logger.info(f"📊 經文總覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
    if final_gaps:
        logger.warning(f"⚠️ 尚有 {len(final_gaps)} 處未完成區段：")
        for idx, g in enumerate(final_gaps, 1):
            logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(g['gap_text']))} 字 | 預覽: 『{g['gap_text'][:40]}...』")
    else:
        logger.info("🎉 經文已 100% 銷文完畢，全文完整無缺！")
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
    seen_indices = {tuple(iss.get("merge_indices", [iss["index"]])) for iss in pre_issues}
    for iss in ai_issues:
        m_idx = tuple(iss.get("merge_indices", [iss.get("index", -1)]))
        if m_idx not in seen_indices and m_idx[0] != -1:
            all_issues.append(iss)
            seen_indices.add(m_idx)

    all_issues = merge_overlapping_issues(all_issues, max_valid_len=len(segments))
    all_issues.sort(key=lambda x: x.get("merge_indices", [x.get("index", 999)])[0])

    review_path = os.path.splitext(output_path)[0] + "_review.json"
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(all_issues, f, ensure_ascii=False, indent=2)

    logger.info(f"\n📊 審查完畢，共發現 {len(all_issues)} 處問題：")
    for iss in all_issues:
        m_str = ",".join(map(str, iss.get('merge_indices', [iss.get('index')])))
        logger.info(f"  段落 [{m_str}] ({iss.get('type','')})：{iss.get('problem','')}")
    logger.info(f"結果報告已儲存至：{review_path}")

    # 精確計算覆蓋率與漏段清單
    remaining_gaps = find_missing_gaps(sutra_text, segments)
    clean_sutra_len = len(normalize_text(sutra_text))
    total_gaps_chars = sum(len(normalize_text(g["gap_text"])) for g in remaining_gaps)
    covered_chars = max(0, clean_sutra_len - total_gaps_chars)
    coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

    logger.info("\n" + "=" * 60)
    logger.info(f"📈 當前經文覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
    if remaining_gaps:
        logger.warning(f"⚠️ 檢測到 {len(remaining_gaps)} 處實質經文漏段：")
        for idx, g in enumerate(remaining_gaps, 1):
            logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(g['gap_text']))} 字 | 預覽: 『{g['gap_text'][:40]}...』")
    else:
        logger.info("🎉 經文已 100% 全覆蓋，無實質漏句。")
    logger.info("=" * 60)
    return all_issues


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
        total_gaps_chars = sum(len(normalize_text(g["gap_text"])) for g in remaining_gaps)
        covered_chars = max(0, clean_sutra_len - total_gaps_chars)
        coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

        logger.info(f"📊 修正後經文覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
        if remaining_gaps:
            logger.warning(f"⚠️ 尚有 {len(remaining_gaps)} 處未銷文漏段：")
            for idx, g in enumerate(remaining_gaps, 1):
                logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(g['gap_text']))} 字 | 預覽: 『{g['gap_text'][:40]}...』")
        else:
            logger.info("🎉 經文已 100% 銷文完畢，無任何遺漏與割裂！")
        logger.info("=" * 65)
    return True


def run_auto(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    segments: List[str],
    style: str,
    output_path: str,
    logger: logging.Logger
) -> None:
    """★ 一鍵全流程模式（審查 -> 修正 -> 重排 -> 覆蓋率統計）"""
    logger.info("=" * 65)
    logger.info(f"★ 啟動一鍵全自動模式（經文風格：{'古典全句號文本' if style == 'ALL_PERIOD' else '現代新標點文本'}）")
    logger.info("=" * 65)

    review_path = os.path.splitext(output_path)[0] + "_review.json"
    all_issues = None

    if os.path.exists(review_path):
        try:
            with open(review_path, "r", encoding="utf-8") as f:
                cached_issues = json.load(f)
            max_idx = max(
                [max(iss.get("merge_indices", [iss.get("index", 0)])) for iss in cached_issues]
            ) if cached_issues else -1
            if max_idx < len(segments):
                all_issues = cached_issues
                logger.info(f"📋 檢測到有效審查報告：{review_path}")
                logger.info(f"⚡ 索引校驗通過（共 {len(segments)} 段），載入報告中 {len(all_issues)} 處問題進行修正...")
            else:
                logger.warning(
                    f"⚠️ 既有審查報告索引已過期（報告最大索引 {max_idx} >= 當前段落數 {len(segments)}），重新審查..."
                )
                all_issues = None
        except Exception as e:
            logger.warning(f"⚠️ 讀取既有審查報告失敗 ({e})，重新執行審查流程...")
            all_issues = None

    if all_issues is None:
        all_issues = run_review(args, client, model, sutra_text, segments, style, output_path, logger)
        if all_issues is None:
            logger.error("❌ 審查中斷，停止流程。")
            return

    if not all_issues:
        logger.info("\n✅ 審查結果良好，未發現需要修正的段落。")
        return

    fix_success = run_fix(args, client, model, sutra_text, segments, output_path, logger, all_issues=all_issues)
    if not fix_success or args.dry_run:
        return

    logger.info("\n" + "=" * 65)
    logger.info("🎉 一鍵全自動模式執行完畢，所有檢出問題已處理完畢！")
    logger.info(f"最終輸出檔案：{output_path}")
    logger.info("=" * 65)

    latest_segments = extract_segments_from_md(output_path)
    remaining_gaps = find_missing_gaps(sutra_text, latest_segments)

    clean_sutra_len = len(normalize_text(sutra_text))
    total_gaps_chars = sum(len(normalize_text(g["gap_text"])) for g in remaining_gaps)
    covered_chars = max(0, clean_sutra_len - total_gaps_chars)
    coverage_rate = (covered_chars / max(1, clean_sutra_len)) * 100

    logger.info("\n" + "=" * 65)
    logger.info(f"📊 最終經文覆蓋率: {coverage_rate:.2f}% ({covered_chars}/{clean_sutra_len} 純漢字)")
    if remaining_gaps:
        logger.warning(f"⚠️ 尚有 {len(remaining_gaps)} 處未銷文漏段：")
        for idx, g in enumerate(remaining_gaps, 1):
            logger.warning(f"  [{idx}] 遺漏字數: {len(normalize_text(g['gap_text']))} 字 | 預覽: 『{g['gap_text'][:40]}...』")
    else:
        logger.info("🎉 經文已 100% 銷文完畢，無任何遺漏與割裂！")
    logger.info("=" * 65)


# ============================================================
#  十、智慧狀態機與全自動流水線控制器
# ============================================================
class PipelineState(Enum):
    NEED_GENERATE = "全本銷文 (Generate)"
    NEED_GAP_FILL_1 = "初次補漏 (Gap Fill 1)"
    NEED_CHECKPOINT_FIX = "接續未完修復 (Resume Checkpoint)"
    NEED_REVIEW_FIX = "依審查報告修復 (Execute Review Fix)"
    NEED_AI_REVIEW = "AI 深度審查 (AI Review)"
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
        max_idx = max([max(iss.get("merge_indices", [iss.get("index", 0)])) for iss in issues])
        return max_idx < len(segments)
    except Exception:
        return False


def detect_current_state(
    sutra_text: str,
    output_path: str,
    logger: logging.Logger
) -> Tuple[PipelineState, Dict[str, Any]]:
    """
    【智慧狀態感知器】依據檔案現況自動判定流水線切入點：
    1. 有 Checkpoint -> 優先接續修復
    2. 無 MD 或 MD 為空 -> 啟動全本銷文
    3. 有 MD 但有實質漏字 -> 進入初次補漏
    4. 覆蓋率 100% 且有有效 review.json -> 進入 Fix 修復
    5. 覆蓋率 100% 且無審查報告 -> 進入 AI Review
    """
    checkpoint_path = os.path.splitext(output_path)[0] + "_checkpoint.json"
    review_path = os.path.splitext(output_path)[0] + "_review.json"

    # 1. 優先檢查是否有修復到一半的 Checkpoint 暫存檔
    if os.path.exists(checkpoint_path) and os.path.exists(review_path):
        cached = load_checkpoint(checkpoint_path)
        if cached:
            logger.info("🔍 [狀態感知] 發現中斷的修復暫存檔 (Checkpoint)，優先接續修復！")
            return PipelineState.NEED_CHECKPOINT_FIX, {"checkpoint_path": checkpoint_path}

    # 2. 檢查 MD 銷文檔是否存在
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        logger.info("🔍 [狀態感知] 未發現銷文 MD 檔案，將從頭啟動【全本銷文】。")
        return PipelineState.NEED_GENERATE, {}

    segments = extract_segments_from_md(output_path)
    if not segments:
        logger.info("🔍 [狀態感知] 銷文 MD 檔案無有效段落，將從頭啟動【全本銷文】。")
        return PipelineState.NEED_GENERATE, {}

    # 3. 計算覆蓋率與漏段
    gaps = find_missing_gaps(sutra_text, segments)
    clean_len = len(normalize_text(sutra_text))
    gap_chars = sum(len(normalize_text(g["gap_text"])) for g in gaps)
    coverage = ((clean_len - gap_chars) / max(1, clean_len)) * 100

    # ★ 修正：只要有任何實質漏字就進入補漏，不再依賴 99.8% 百分比門檻
    if gaps and gap_chars > 0:
        logger.info(
            f"🔍 [狀態感知] 檢測到 MD 檔案已存在，但尚有 {len(gaps)} 處漏段"
            f"（實質漏字 {gap_chars} 字，覆蓋率 {coverage:.1f}%），進入【初次補漏】。"
        )
        return PipelineState.NEED_GAP_FILL_1, {"gaps": gaps, "coverage": coverage}

    # 4. 覆蓋率已達 100%，檢查是否有未執行的 review.json
    if is_review_json_valid(review_path, output_path, segments):
        with open(review_path, "r", encoding="utf-8") as f:
            issues = json.load(f)
        if issues:
            logger.info(f"🔍 [狀態感知] 檢測到有效的審查報告 ({len(issues)} 處問題)，直接進入【修復階段】。")
            return PipelineState.NEED_REVIEW_FIX, {"issues": issues}
        else:
            logger.info("🔍 [狀態感知] 既有審查報告顯示 0 問題且覆蓋率 100%，流程已圓滿完成！")
            return PipelineState.COMPLETED, {}

    # 5. 覆蓋率已達 100% 且無審查報告，進入 AI Review
    logger.info(f"🔍 [狀態感知] 經文覆蓋率已達 100% ({len(segments)} 段)，進入【AI 深度審查】。")
    return PipelineState.NEED_AI_REVIEW, {"segments": segments}


def run_pipeline(
    args: argparse.Namespace,
    client: OpenAI,
    model: str,
    sutra_text: str,
    style: str,
    output_path: str,
    logger: logging.Logger
) -> None:
    """
    【四階段閉環智慧流水線】
    銷文 (Generate) -> 初次補漏 (Gap Fill 1) -> AI 審查與修復 (Review & Fix) -> 二次補漏 (Gap Fill 2)
    """
    logger.info("=" * 68)
    logger.info("🌟 啟動佛經銷文智慧閉環流水線")
    logger.info(f"   經文檔案：{args.file}")
    logger.info(f"   輸出檔案：{output_path}")
    logger.info("=" * 68)

    last_state = None
    last_covered_chars = -1
    stall_count = 0

    while True:
        state, meta = detect_current_state(sutra_text, output_path, logger)

        cur_segs = extract_segments_from_md(output_path) if os.path.exists(output_path) else []
        cur_gaps = find_missing_gaps(sutra_text, cur_segs)
        cur_covered = len(normalize_text(sutra_text)) - sum(len(normalize_text(g["gap_text"])) for g in cur_gaps)

        if state == last_state and cur_covered == last_covered_chars and state != PipelineState.COMPLETED:
            stall_count += 1
            if stall_count >= 2:
                logger.error(f"\n❌ [防死鎖警報] 流水線在【{state.value}】階段未產生實質進展，安全中止以防耗費額度。")
                logger.warning(f"👉 請檢查日誌確認 API 是否正常，或手動檢視：{output_path}")
                break
        else:
            stall_count = 0

        last_state = state
        last_covered_chars = cur_covered

        if state == PipelineState.COMPLETED:
            review_path = os.path.splitext(output_path)[0] + "_review.json"
            if os.path.exists(review_path):
                try:
                    os.remove(review_path)
                except Exception:
                    pass
            logger.info("\n" + "=" * 68)
            logger.info("🎉🎉🎉 全流程圓滿完成！經文 100% 全文覆蓋，且已通過因明文法深度審查！")
            logger.info(f"最終成果：{output_path}")
            logger.info("=" * 68)
            break

        elif state == PipelineState.NEED_GENERATE:
            logger.info("\n🚀 === [Stage 1/4] 開始全本銷文 ===")
            run_generate(args, client, model, sutra_text, output_path, logger)

        elif state == PipelineState.NEED_GAP_FILL_1:
            logger.info("\n🛠️ === [Stage 2/4] 執行初次補漏（確保 Review 前全文到位）===")
            segments = extract_segments_from_md(output_path)
            run_fix_gaps(args, client, model, sutra_text, segments, style, output_path, logger)

        elif state == PipelineState.NEED_AI_REVIEW:
            logger.info("\n🧐 === [Stage 3a/4] 執行 AI 深度邏輯與科判審查 ===")
            segments = extract_segments_from_md(output_path)
            issues = run_review(args, client, model, sutra_text, segments, style, output_path, logger)
            if issues is None:
                logger.error("❌ 審查過程遭遇錯誤，流水線已安全暫停。")
                break
            if not issues:
                logger.info("✨ 經審查無任何語意割裂，品質極佳！")
                review_path = os.path.splitext(output_path)[0] + "_review.json"
                with open(review_path, "w", encoding="utf-8") as f:
                    json.dump([], f)
                continue

        elif state in (PipelineState.NEED_REVIEW_FIX, PipelineState.NEED_CHECKPOINT_FIX):
            logger.info("\n🔧 === [Stage 3b/4] 執行段落合併與瑕疵重寫 ===")
            segments = extract_segments_from_md(output_path)
            issues_to_fix = meta.get("issues")
            if issues_to_fix and args.max_fix == 50:
                args.max_fix = len(issues_to_fix)
            success = run_fix(args, client, model, sutra_text, segments, output_path, logger, all_issues=issues_to_fix)
            if not success:
                logger.warning("⚠️ 修復中斷，已保存現有進度。隨時再次執行原指令即可秒級接續。")
                break

            logger.info("\n🔍 === [Stage 4/4] 執行二次安全補漏（防止重切產生的微型縫隙）===")
            latest_segs = extract_segments_from_md(output_path)
            final_gaps = find_missing_gaps(sutra_text, latest_segs)
            if final_gaps:
                logger.info(f"檢測到修復後產生 {len(final_gaps)} 處微小縫隙，立即自動補齊...")
                run_fix_gaps(args, client, model, sutra_text, latest_segs, style, output_path, logger)
            else:
                logger.info("✅ 完美！修復後經文依然 100% 完整無任何縫隙。")

            review_path = os.path.splitext(output_path)[0] + "_review.json"
            try:
                with open(review_path, "w", encoding="utf-8") as f:
                    json.dump([], f)
            except Exception:
                pass


# ============================================================
#  十一、金鑰讀取與輔助入口
# ============================================================
def load_api_key(key_file: Optional[str] = None, api_key_str: Optional[str] = None, is_opencode: bool = False) -> Optional[str]:
    """讀取 API Key：命令列參數 > 環境變數 > 檔案"""
    if api_key_str and api_key_str.strip():
        return api_key_str.strip()

    env_vars = ["OPENCODE_API_KEY", "OPENAI_API_KEY"] if is_opencode else ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]
    for var in env_vars:
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_files = []
    if key_file:
        candidate_files.append(key_file)
    if is_opencode:
        candidate_files.extend([
            os.path.join(base_dir, "opencode_key.txt"),
            os.path.join(base_dir, "opencode_api_key.txt"),
            os.path.join(base_dir, "api_key.txt"),
        ])
    else:
        candidate_files.extend([
            os.path.join(base_dir, "api_key.txt"),
            os.path.join(base_dir, "deepseek_key.txt"),
            os.path.join(base_dir, "deepseek_api_key.txt"),
            os.path.join(base_dir, "key.txt"),
        ])

    for kf in candidate_files:
        if os.path.exists(kf):
            with open(kf, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key

    target_name = "opencode_key.txt (或 api_key.txt)" if is_opencode else "api_key.txt"
    env_hint = "OPENCODE_API_KEY / OPENAI_API_KEY" if is_opencode else "DEEPSEEK_API_KEY / OPENAI_API_KEY"
    print(f"❌ 找不到金鑰檔案或內容為空，請建立 {target_name} 或設定環境變數 ({env_hint})")
    return None


def auto_output_path(input_file: str) -> str:
    """自動推導 MD 檔路徑"""
    input_abs = os.path.abspath(input_file)
    input_dir = os.path.dirname(input_abs)
    input_name = os.path.splitext(os.path.basename(input_abs))[0]
    return os.path.join(input_dir, f"{input_name}_銷文.md")


def main():
    parser = argparse.ArgumentParser(
        description="DeepSeek 佛經銷文斷句品質深度審查、一鍵修正與專注補漏工具（智慧閉環流水線版）"
    )
    parser.add_argument("--file", type=str, required=True, help="原始經文 txt 檔案路徑")
    parser.add_argument("--output", type=str, default=None, help="目標銷文 md 檔案路徑（預設自動推導）")

    parser.add_argument("--auto", action="store_true", help="★ 智慧全流程模式（預設啟動：銷文->補漏->審查修復->二次補漏）")
    parser.add_argument("--generate", "--gen", action="store_true", help="手動指定：強制全本從頭銷文")
    parser.add_argument("--fix-gaps", "--gaps", action="store_true", help="手動指定：僅執行補漏")
    parser.add_argument("--review", action="store_true", help="手動指定：僅執行 AI 審查並輸出 review.json")
    parser.add_argument("--fix", action="store_true", help="手動指定：僅讀取 review.json 執行修正")
    parser.add_argument("--dry-run", action="store_true", help="預覽待修清單，不呼叫 API 且不更動檔案")

    parser.add_argument("--model", type=str, default="deepseek-v4-flash", help="呼叫模型名稱")
    parser.add_argument("--reasoning-effort", type=str, default="high", choices=["low", "medium", "high"])
    parser.add_argument("--max-fix", type=int, default=50, help="單次最多修正問題數")
    parser.add_argument("--timeout", type=int, default=300, help="單次 API 超時時間（秒）")
    parser.add_argument("--opencode", "--go", action="store_true", dest="opencode", help="★ 使用 OpenCode Go 訂閱端點 (https://opencode.ai/zen/go/v1)")
    parser.add_argument("--zen", action="store_true", help="使用 OpenCode Zen 按量計費端點 (https://opencode.ai/zen/v1)")
    parser.add_argument("--base-url", type=str, default=None, help="自訂 API Base URL")
    parser.add_argument("--api-key", type=str, default=None, help="直接指定 API Key 字串")
    parser.add_argument("--api-key-file", type=str, default=None, help="指定 API Key 檔案路徑")
    args = parser.parse_args()

    has_manual_mode = (args.generate or args.fix_gaps or args.review or args.fix or args.dry_run)
    if not has_manual_mode:
        args.auto = True

    if args.dry_run and not args.fix:
        args.fix = True

    if args.output is None:
        args.output = auto_output_path(args.file)

    log_path = os.path.splitext(args.output)[0] + "_review_log.txt"
    logger = setup_logger(log_path)

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

    is_opencode_provider = args.opencode or args.zen
    if args.zen:
        base_url = args.base_url or "https://opencode.ai/zen/v1"
        provider_name = "OpenCode Zen 按量計費 (https://opencode.ai/zen/v1)"
    elif args.opencode:
        base_url = args.base_url or "https://opencode.ai/zen/go/v1"
        provider_name = "OpenCode Go 訂閱端點 (https://opencode.ai/zen/go/v1)"
    else:
        base_url = args.base_url or "https://api.deepseek.com"
        provider_name = "DeepSeek 官方 API"

    api_key = load_api_key(key_file=args.api_key_file, api_key_str=args.api_key, is_opencode=is_opencode_provider)
    if not api_key:
        return

    logger.info(f"API 提供商: {provider_name} ({base_url}) | 模型: {args.model}")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout)

    if args.auto and not (args.generate or args.fix_gaps or args.review or (args.fix and not args.dry_run)):
        run_pipeline(args, client, args.model, sutra_text, style, args.output, logger)
    elif args.generate:
        run_generate(args, client, args.model, sutra_text, args.output, logger)
    elif args.fix_gaps:
        run_fix_gaps(args, client, args.model, sutra_text, segments, style, args.output, logger)
    elif args.review:
        run_review(args, client, args.model, sutra_text, segments, style, args.output, logger)
    elif args.fix or args.dry_run:
        run_fix(args, client, args.model, sutra_text, segments, args.output, logger)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 已偵測到中斷信號 (Ctrl + C)，程式已安全停止。")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)