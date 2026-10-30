"""剪贴板导出：写纯代码列表，同花顺自定义板块 Ctrl+V 直接导入

主链路不做 RPA，规避风控。同花顺侧操作（用户手动，一次性设置）:
  自选股页面 -> 右键 -> 批量操作/导入 -> 粘贴
"""
from __future__ import annotations

from src.config import Config
from src.log import get_logger

log = get_logger(__name__)


class ClipboardUnavailable(RuntimeError):
    """剪贴板不可用（缺 pyperclip / 被系统或安全软件拒绝写入）。"""


def build_code_list(rows: list[dict], cfg: Config) -> str:
    """全局去重后生成代码清单文本"""
    dedup = cfg.get("export.dedup_global", True)
    sep = "\n" if cfg.get("export.clipboard_separator", "newline") == "newline" else ","
    seen: set[str] = set()
    codes: list[str] = []
    for r in rows:
        code = r["code"]
        if dedup and code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return sep.join(codes)


def build_code_groups(rows: list[dict], cfg: Config) -> list[str]:
    """按同花顺一次粘贴的字数上限分组，返回各组文本列表。

    每只代码 6 位 + 分隔符，超出上限的题材自动切成多组，逐组 Ctrl+V 即可。
    """
    dedup = cfg.get("export.dedup_global", True)
    sep = "\n" if cfg.get("export.clipboard_separator", "newline") == "newline" else ","
    limit = int(cfg.get("export.clipboard_chunk_size", 200) or 200)
    seen: set[str] = set()
    groups: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for r in rows:
        code = r["code"]
        if dedup and code in seen:
            continue
        seen.add(code)
        add = len(code) + (len(sep) if cur else 0)
        if cur and cur_len + add > limit:
            groups.append(sep.join(cur))
            cur, cur_len = [code], len(code)
        else:
            cur.append(code)
            cur_len += add
    if cur:
        groups.append(sep.join(cur))
    return groups


def copy_to_clipboard(text: str) -> int:
    """写入系统剪贴板，返回字符数。

    失败一律转成 ClipboardUnavailable：裸 500 会让前端只看到
    「Internal Server Error」，用户不知道该改用导出 TXT。
    """
    try:
        import pyperclip          # 打包环境可能没装，ImportError 也要接住

        pyperclip.copy(text)
    except Exception as e:
        raise ClipboardUnavailable(
            f"剪贴板写入失败（{e.__class__.__name__}），请改用「生成 TXT」"
        ) from e
    log.info("已写入剪贴板 %d 字符", len(text))
    return len(text)
