"""读时迁移的供应商命名，以及迁移后密钥不能断。

两条红线（本文件就是它们的存在理由）：
1. 供应商的名字必须**从端点推导**，不能取第一个老档案的 id——本机实例里
   同一台中转站上的两个模型曾被归到一个叫 ``hunyuan4preview`` 的供应商下，
   看上去像"这个供应商只有这一个模型"，语义完全错；
2. 迁移改了 provider id，但 ``keys.local.json`` 里的密钥仍挂在**老档案 id** 名下。
   密钥候选链覆盖不到老 id，用户就会看到"明明存过密钥，界面却说没配"。

已保存成新结构（``providers`` 数组）的配置不走迁移，也不二次改名。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if CONSOLE_DIR not in sys.path:
    sys.path.insert(0, CONSOLE_DIR)

from harness import chat, config, keyring, runs  # noqa: E402

#: 本机那把密钥（只落在临时密钥文件里，永不写真实 keys.local.json）
FAKE_KEY = "sk-migration-test-NEVER-LEAK"
#: 迁移前的两个老档案：同端点，按老结构各写一遍 base_url
LEGACY_MODELS = [
    {"id": "hunyuan4preview", "protocol": "openai", "api_mode": "chat_completions",
     "base_url": "http://127.0.0.1:20128/v1", "model": "cbcn/hy4-preview",
     "key_masked": "sk-1****8551", "key_env": ""},
    {"id": "cbcn-deepseek-v4-1-flash", "protocol": "openai", "api_mode": "chat_completions",
     "base_url": "http://127.0.0.1:20128/v1", "model": "cbcn/deepseek-v4.1-flash",
     "key_masked": "sk-1****8551", "key_env": ""},
]


def _legacy_entry(archive_id, base_url, model=""):
    return {"id": archive_id, "protocol": "openai", "api_mode": "chat_completions",
            "base_url": base_url, "model": model or archive_id,
            "key_masked": "sk-1****8551", "key_env": ""}


@pytest.fixture
def clean_env(monkeypatch):
    """清掉会左右「密钥状态」结论的环境变量（含按新旧 id 推出的那些）。"""
    for name in ("LOCAL_20128_API_KEY", "HUNYUAN4PREVIEW_API_KEY",
                 "CBCN_DEEPSEEK_V4_1_FLASH_API_KEY", "MODEL_LOCAL_20128_API_KEY",
                 "MODEL_CBCN_HY4_PREVIEW_API_KEY", "DEEPSEEK_API_KEY",
                 "API_DEEPSEEK_AI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def temp_keys(monkeypatch, tmp_path):
    """把本机密钥文件指到临时目录，绝不写真实 keys.local.json。"""
    shadow = tmp_path / "keys.local.json"
    monkeypatch.setattr(keyring, "path", lambda: str(shadow))
    return shadow


def _load_legacy(monkeypatch, tmp_path, models, providers=None):
    """写一份老结构 config.json 到临时路径，读回来（走完整读时迁移）。

    ``runs_root`` / ``sandbox_root`` **必须**一起指到 tmp_path：只 patch
    ``CONFIG_PATH`` 是不够的，``config.load()`` 会把这两项按默认值解析到真实的
    ``runs/`` 与 ``sandboxes/``，删除类用例就会真删本机的运行记录
    （2026-10-02 真实事故，见 conftest.forbid_writing_real_config）。
    """
    raw = {"models": list(models)}
    if providers is not None:
        raw["providers"] = providers
    sandbox_root = tmp_path / "sandboxes"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    raw["sandbox_root"] = str(sandbox_root)
    raw["runs_root"] = str(tmp_path / "runs")
    raw["packs_root"] = str(tmp_path / "packs")
    raw["snapshot_cache"] = str(sandbox_root / ".snapshots")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return config.load()


# ---------------------------------------------------------------- 命名规则

def test_vendor_is_named_from_the_endpoint_not_the_archive_id(monkeypatch, tmp_path):
    """api.deepseek.com 上的档案叫 deepseek，不叫它自己的档案 id。"""
    cfg = _load_legacy(monkeypatch, tmp_path, [
        _legacy_entry("我的主力模型", "https://api.deepseek.com/v1"),
    ])
    (provider,) = cfg["providers"]
    assert provider["id"] == "deepseek"
    assert provider["display_name"] == "deepseek"
    # 老档案 id 仍然记着：密钥还挂在它名下，记分板的归属也靠它
    assert provider["legacy_ids"] == ["我的主力模型"]


def test_local_relay_is_named_from_host_and_port(monkeypatch, tmp_path):
    """本机中转推不出厂商名，用 host+端口认人，并在显示名里说明是本机中转。"""
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    assert cfg["providers_migrated"] is True
    (provider,) = cfg["providers"]
    assert provider["id"] == "local-20128"
    assert provider["display_name"] == "本机中转 127.0.0.1:20128"
    assert provider["base_url"] == "http://127.0.0.1:20128/v1"
    # 两个模型仍在同一张卡下，老档案 id 一个不少
    assert [m["id"] for m in provider["models"]] == ["cbcn/hy4-preview", "cbcn/deepseek-v4.1-flash"]
    assert provider["legacy_ids"] == ["hunyuan4preview", "cbcn-deepseek-v4-1-flash"]
    # 记录归属不受影响：老档案 id 仍在，qualified_id 跟着新供应商走
    assert [m["legacy_ids"] for m in cfg["models"]] == [provider["legacy_ids"]] * 2


def test_lan_relay_is_kept_apart_from_loopback(monkeypatch, tmp_path):
    """内网中转与本机中转分开命名：同端口的两台机器不该撞成一个名字。"""
    cfg = _load_legacy(monkeypatch, tmp_path, [
        _legacy_entry("a", "http://127.0.0.1:8080/v1"),
        _legacy_entry("b", "http://192.168.1.50:8080/v1"),
    ])
    names = {p["id"]: p["display_name"] for p in cfg["providers"]}
    assert names["local-8080"] == "本机中转 127.0.0.1:8080"
    assert names["lan-192-168-1-50-8080"] == "内网中转 192.168.1.50:8080"


def test_name_conflicts_get_a_stable_suffix(monkeypatch, tmp_path):
    """两个端点推成同一个名字时加短后缀，且与出现顺序无关。"""
    models = [
        _legacy_entry("m1", "https://api.deepseek.com/v1"),
        _legacy_entry("m2", "https://api.deepseek.ai/v1"),
    ]
    forward = _load_legacy(monkeypatch, tmp_path, models)["providers"]
    reverse = _load_legacy(monkeypatch, tmp_path, list(reversed(models)))["providers"]

    ids = {p["id"] for p in forward}
    assert len(ids) == 2, "重名必须被消解开"
    assert all(i.startswith("deepseek-") for i in ids)
    assert {p["id"] for p in forward} == {p["id"] for p in reverse}, "顺序不该影响名字"
    # 显示名也得分得开：写清是哪个端点，不掺哈希
    displays = {p["display_name"] for p in forward}
    assert displays == {"deepseek（api.deepseek.com）", "deepseek（api.deepseek.ai）"}


def test_migration_is_idempotent_for_the_same_endpoint(monkeypatch, tmp_path):
    """同一份老配置迁移几次都得到同一个名字（读时迁移每次启动都会跑一遍）。"""
    def _names(cfg):
        return {p["base_url"]: (p["id"], p["display_name"]) for p in cfg["providers"]}

    once = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    twice = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    reordered = _load_legacy(monkeypatch, tmp_path, list(reversed(LEGACY_MODELS)))
    assert _names(once) == _names(twice) == _names(reordered)
    assert _names(once)["http://127.0.0.1:20128/v1"] == ("local-20128", "本机中转 127.0.0.1:20128")


def test_archive_without_endpoint_falls_back_to_its_id(monkeypatch, tmp_path):
    """没填 base_url 的老档案推不出名字，只能沿用档案 id（它唯一的稳定标识）。"""
    cfg = _load_legacy(monkeypatch, tmp_path, [_legacy_entry("只有名字", "")])
    (provider,) = cfg["providers"]
    assert provider["id"] == "只有名字"
    assert provider["display_name"] == "只有名字"


def test_derived_ids_satisfy_the_frontend_id_pattern(clean_env):
    """推导出的 id 要能过表单校验（models.js 的 ID_PATTERN）与 util.sanitize_id。"""
    import re
    pattern = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    urls = ["https://api.deepseek.com/v1", "http://127.0.0.1:20128/v1",
            "http://192.168.1.50:8080/v1", "https://api.example.co.uk/v1",
            "https://1password.example.com/v1", "http://10.0.0.7:11434/v1"]
    for url in urls:
        pid = config._provider_identity(url)[0]
        assert pattern.match(pid), "%s → %s 不合法" % (url, pid)


# ---------------------------------------------------------------- 密钥不断

def test_key_owner_candidates_cover_legacy_ids():
    """本机密钥文件的候选链：新供应商 id 在前，老档案 id 一个不落。"""
    assert chat.key_owner_candidates("local-20128",
                                     ["hunyuan4preview", "cbcn-deepseek-v4-1-flash"]) == [
        "local-20128", "hunyuan4preview", "cbcn-deepseek-v4-1-flash"]
    # 去重：新 id 与老 id 撞名时只留一份
    assert chat.key_owner_candidates("x", ["x", " y ", ""]) == ["x", "y"]
    assert chat.key_owner_candidates("", ["old"]) == ["old"]


def test_env_candidates_cover_legacy_ids(monkeypatch, tmp_path, clean_env):
    """环境变量候选同样覆盖老档案 id：按老名字配过的变量不该失联。"""
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    candidates = chat.key_candidates(cfg["models"][0])
    assert candidates[:3] == ["LOCAL_20128_API_KEY", "HUNYUAN4PREVIEW_API_KEY",
                              "CBCN_DEEPSEEK_V4_1_FLASH_API_KEY"]
    # 供应商级仍然压过模型级：同一中转站多个模型共用一把
    assert candidates.index("LOCAL_20128_API_KEY") < candidates.index(
        "MODEL_CBCN_HY4_PREVIEW_API_KEY")


def test_stored_key_is_found_under_a_legacy_id(monkeypatch, tmp_path, clean_env, temp_keys):
    """迁移后按新 id 查不到、按老 id 查得到——密钥不许断。"""
    keyring.set_key("hunyuan4preview", FAKE_KEY)
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    assert cfg["providers"][0]["id"] == "local-20128"
    # 展开后的档案带着 legacy_ids，chat 的取密钥路径才找得到老 id
    assert chat._stored_key(cfg["models"][0]) == FAKE_KEY
    assert chat._model_key(cfg["models"][0]) == FAKE_KEY
    # 新 id 下确实没有密钥——这条用例才真的验到了"回退"而不是碰巧命中
    assert not keyring.get_key("local-20128")


def test_provider_card_reports_the_key_stored_under_a_legacy_id(
        monkeypatch, tmp_path, clean_env, temp_keys):
    """模型档案页的供应商卡必须仍显示「✓ 已存密钥」。"""
    keyring.set_key("hunyuan4preview", FAKE_KEY)
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    out = runs.list_providers(cfg)
    (provider,) = out["providers"]
    assert out["migrated"] is True          # 迁移提示条照常
    assert provider["id"] == "local-20128"
    assert provider["key_present"] is True
    assert provider["key_stored"] is True
    assert provider["key_masked"] == "sk-1****8551"
    # 取值本身不出服务端
    assert FAKE_KEY not in json.dumps(out, ensure_ascii=False)


def test_provider_card_says_missing_when_no_key_anywhere(
        monkeypatch, tmp_path, clean_env, temp_keys):
    """没有密钥就说没有，不能因为候选链放宽就一路绿灯。"""
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    (provider,) = runs.list_providers(cfg)["providers"]
    assert provider["key_present"] is False
    assert provider["key_stored"] is False


def test_saving_a_migrated_provider_keeps_the_legacy_aliases(
        monkeypatch, tmp_path, clean_env, temp_keys):
    """编辑保存一次不能把 legacy_ids 弄丢——那等于把密钥锁在门外。"""
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    saved = []
    monkeypatch.setattr(config, "update_providers",
                        lambda providers: saved.append(json.loads(json.dumps(providers))))
    provider = cfg["providers"][0]
    runs.upsert_provider(cfg, {
        "id": provider["id"], "display_name": "我自己起的名字",
        "protocol": "openai", "api_mode": "chat_completions",
        "base_url": provider["base_url"], "key_masked": provider["key_masked"],
        "previous_id": provider["id"],
        "models": [{"id": m["id"]} for m in provider["models"]],
    })
    (written,) = saved[0]          # saved[0] 是整份 providers 列表
    assert written["display_name"] == "我自己起的名字"
    assert written["legacy_ids"] == ["hunyuan4preview", "cbcn-deepseek-v4-1-flash"]
    # 密钥仍按老档案 id 找得到
    keyring.set_key("hunyuan4preview", FAKE_KEY)
    flat = config.expand_models([config._normalize_provider(written, 0)])
    assert chat._stored_key(flat[0]) == FAKE_KEY


def test_deleting_a_provider_clears_its_legacy_key_aliases(
        monkeypatch, tmp_path, clean_env, temp_keys):
    """删供应商要连老档案 id 名下的密钥一起清，否则明文永远留在本机。"""
    keyring.set_key("hunyuan4preview", FAKE_KEY)
    keyring.set_key("cbcn-deepseek-v4-1-flash", FAKE_KEY)
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    monkeypatch.setattr(config, "update_providers", lambda providers: None)
    runs.delete_provider(cfg, "local-20128")
    assert keyring.load() == {}


def test_deleting_a_provider_leaves_another_suppliers_key_alone(
        monkeypatch, tmp_path, clean_env, temp_keys):
    """别删到别人的：重叠的老档案 id 仍被别的供应商引用时跳过。"""
    keyring.set_key("hunyuan4preview", FAKE_KEY)
    cfg = _load_legacy(monkeypatch, tmp_path, LEGACY_MODELS)
    # 另一个供应商把同一条老档案 id 记在自己名下（迁移链条可能交叠）
    cfg["providers"].append({
        "id": "other", "display_name": "other", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://other.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768,
        "legacy_ids": ["hunyuan4preview"], "models": [{"id": "o", "name": "o"}],
    })
    monkeypatch.setattr(config, "update_providers", lambda providers: None)
    runs.delete_provider(cfg, "local-20128")
    assert "hunyuan4preview" in keyring.load()


# ---------------------------------------------------------------- 新结构不动

def test_providers_structure_is_left_alone(monkeypatch, tmp_path):
    """已保存成新结构的配置不走迁移：不改名、不注入 legacy_ids。"""
    provider = {"id": "hunyuan4preview", "display_name": "我自己起的名字",
                "protocol": "openai", "api_mode": "chat_completions",
                "base_url": "http://127.0.0.1:20128/v1",
                "default_context_window": 262144, "default_max_tokens": 32768,
                "models": [{"id": "cbcn/hy4-preview"}]}
    cfg = _load_legacy(monkeypatch, tmp_path,
                       LEGACY_MODELS, providers=[provider])
    assert cfg["providers_migrated"] is False
    (kept,) = cfg["providers"]
    assert kept["id"] == "hunyuan4preview"          # 存量配置不二次改名
    assert kept["display_name"] == "我自己起的名字"
    assert "legacy_ids" not in kept
    assert [m["id"] for m in cfg["models"]] == ["cbcn/hy4-preview"]


def test_providers_structure_is_untouched_by_resolve_providers():
    """resolve_providers 命中 providers 就原样归一，不去端点推名字。"""
    providers = config.resolve_providers({
        "providers": [{"id": "01", "display_name": "本机中转", "protocol": "openai",
                       "base_url": "http://127.0.0.1:20128/v1", "models": [{"id": "m"}]}],
        "models": LEGACY_MODELS,
    })
    assert [(p["id"], p["display_name"]) for p in providers] == [("01", "本机中转")]
