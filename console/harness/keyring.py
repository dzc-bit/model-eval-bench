"""本机模型密钥存取（console/keys.local.json，永不入库）。

设计依据：用户在「模型档案」页直接粘贴密钥。明文只落在 config.json 旁边的
独立密钥文件里（.gitignore 已排除），config.json 与 API 响应只携带脱敏值。
路径始终取 config.CONFIG_PATH 所在目录：测试把 CONFIG_PATH 重定向到临时
目录时，密钥文件同样被隔离，不会读写真实档案。
"""

from __future__ import annotations

import os
import threading

from . import config, errors, util

_WRITE_LOCK = threading.RLock()
_FILENAME = "keys.local.json"


def path() -> str:
    """密钥文件落点：与 config.json 同目录。"""
    return os.path.join(os.path.dirname(os.path.abspath(config.CONFIG_PATH)), _FILENAME)


def load() -> dict:
    data = util.read_json(path(), default={})
    return data if isinstance(data, dict) else {}


def get_key(model_id: str) -> str:
    """取某档案已粘贴的密钥；没有就返回空串。"""
    return str(load().get(str(model_id), "")).strip()


def set_key(model_id: str, api_key: str) -> str:
    """保存明文密钥，返回脱敏值（config.json 只存这个）。"""
    key = str(api_key or "").strip()
    if not key:
        raise errors.HarnessError(errors.E_MODEL_INVALID, "密钥内容为空，请粘贴有效密钥。")
    with _WRITE_LOCK:
        keys = load()
        keys[str(model_id)] = key
        util.write_json_atomic(path(), keys)
    return mask(key)


def remove_key(model_id: str) -> None:
    with _WRITE_LOCK:
        keys = load()
        if str(model_id) in keys:
            del keys[str(model_id)]
            util.write_json_atomic(path(), keys)


def rename_key(old_id: str, new_id: str) -> None:
    """档案改编号时把密钥跟着搬过去；旧档案本来没有密钥就什么都不做。"""
    if str(old_id) == str(new_id):
        return
    with _WRITE_LOCK:
        keys = load()
        if str(old_id) not in keys:
            return
        keys[str(new_id)] = keys.pop(str(old_id))
        util.write_json_atomic(path(), keys)


def mask(api_key: str) -> str:
    """脱敏展示：保留前 4 后 4，短的整段打码。"""
    key = str(api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return "%s****%s" % (key[:4], key[-4:])
