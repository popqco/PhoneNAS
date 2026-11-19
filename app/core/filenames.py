from __future__ import annotations

import re
import unicodedata


LEGACY = 1
LITERAL = 2

# rclone 1.76.0: the local mask exactly equals encoder.Standard, so rclone
# bypasses both directions of the local conversion. WebDAV with vendor=other
# and None also bypasses conversion. Names, CLI paths, and filter expressions
# therefore have the same spelling. Raw is a different mask, not a bypass.
LITERAL_FLAGS = ("--local-encoding", "Slash,Ctl,Del,Dot", "--webdav-encoding", "None",
                 "--no-unicode-normalization", "--local-unicode-normalization=false")

_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)
# Reject spellings that the original Windows backend or hotfix1 can interpret
# differently. A v1 state file cannot tell which of those clients wrote it.
_STANDARD_SYMBOLS = "\x7f\u201b\u2421\uff0f" + "".join(map(chr, range(0x2400, 0x2420)))
_LEGACY_AMBIGUOUS = frozenset(_STANDARD_SYMBOLS + "\u200b\u2420\uff0e\uff02\uff0a\uff1a\uff1c\uff1e\uff1f\uff3c\uff5c")


def display_path(path: str) -> str:
    """Keep names readable while exposing controls and invisible Unicode safely."""
    return "".join(
        f"<U+{ord(char):04X}>" if unicodedata.category(char).startswith("C")
        or char in ("\u2028", "\u2029") else char for char in path
    )


def relative_parts(path: str) -> tuple[str, ...]:
    """A portable spelling with no Win32 canonicalization or ADS aliases."""
    if not isinstance(path, str) or not path:
        raise ValueError("文件路径必须是非空的相对路径")
    if "\\" in path:
        raise ValueError(f"文件路径不能包含反斜杠：{display_path(path)}")
    for char in path:
        category = unicodedata.category(char)
        # DEL is permitted by Win32; the other control characters are not
        # supported by PhoneNAS. Keep the format-character allowlist narrow.
        if ((category == "Cc" and char != "\x7f") or category == "Cs"
                or (category == "Cf" and char not in "\u200b\u200c\u200d")):
            raise ValueError(
                f"文件或文件夹名称含不支持的字符 U+{ord(char):04X}：{display_path(path)}。"
                "请先检查该名称；软件没有改名或跳过文件。"
            )
    parts = tuple(path.split("/"))
    for part in parts:
        if not part or part in (".", "..") or part.endswith((".", " ")):
            raise ValueError(f"文件路径包含空段、上级路径或末尾点/空格：{display_path(path)}")
        if any(char in '<>:"|?*' for char in part):
            raise ValueError(f"文件路径包含 Windows 非法字符：{display_path(path)}")
        if _RESERVED.fullmatch(part.split(".", 1)[0].rstrip(" .")):
            raise ValueError(f"文件路径包含 Windows 保留设备名：{display_path(path)}")
    return parts


def portable_name_key(name: str) -> str:
    """Compare aliases without ever using the comparison key as a path."""
    return unicodedata.normalize("NFC", name).casefold()


def is_legacy_ambiguous(path: str) -> bool:
    return not _LEGACY_AMBIGUOUS.isdisjoint(path)


def require_legacy_paths(paths, *, location: str) -> None:
    """Prove v1 path spellings cannot be reinterpreted by another client."""
    for path in paths:
        if is_legacy_ambiguous(path):
            raise RuntimeError(
                f"旧数据集的文件名协议无法安全解释{location}：{display_path(path)}。"
                "没有转换或移动文件。请保留两端、历史、基线和保护锁；备份核对后另建任务，使用新的独立数据集。"
            )


def require_legacy_rules(rules: tuple[str, ...]) -> None:
    # hotfix1 rewrote paths even in character classes and ranges. Merely
    # having no affected name in the filtered inventory cannot prove parity.
    symbols = frozenset(_STANDARD_SYMBOLS + "\uff0e")
    for rule in rules:
        if not symbols.isdisjoint(rule):
            raise RuntimeError(
                f"旧数据集的文件名协议无法保证这条排除规则的匹配范围：{display_path(rule)}。"
                "原规则未更改，本次没有写入；请先备份核对或使用新的独立数据集。"
            )
