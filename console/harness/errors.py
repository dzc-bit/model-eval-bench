"""harness 错误码与异常。

设计依据：设计文档 §14「错误码与用户文案分离」——后端只回稳定 code + 中文 message，
前端按 code 映射文案（core/strings.js），未知码显示通用文案。

code 一经发布不再改动语义；新增错误码追加在末尾。
"""

from __future__ import annotations

# ---- 成功 ----
OK = "OK"

# ---- 请求与路由 ----
E_BAD_REQUEST = "E_BAD_REQUEST"              # 参数缺失或类型不对
E_NOT_FOUND = "E_NOT_FOUND"                  # 静态资源或路由不存在
E_METHOD_NOT_ALLOWED = "E_METHOD_NOT_ALLOWED"

# ---- 任务包 ----
E_TASK_NOT_FOUND = "E_TASK_NOT_FOUND"        # packs 里没有这道题
E_TASK_INVALID = "E_TASK_INVALID"            # meta.json 不符合任务包规范

# ---- 模型档案 ----
E_MODEL_NOT_FOUND = "E_MODEL_NOT_FOUND"      # 模型档案不存在
E_MODEL_INVALID = "E_MODEL_INVALID"          # 模型档案字段不合法
E_CHAT_UNSUPPORTED = "E_CHAT_UNSUPPORTED"    # 模型协议或接口形态未接入
E_CHAT_FAILED = "E_CHAT_FAILED"              # 模型代理请求失败

# ---- 运行记录 ----
E_RUN_NOT_FOUND = "E_RUN_NOT_FOUND"          # 运行记录不存在
E_RUN_BUSY = "E_RUN_BUSY"                    # 该轮正在校验，拒绝重复触发
E_RUN_CANCELLED = "E_RUN_CANCELLED"          # 该轮已被批次取消，拒绝启动校验
E_STORE_FAILED = "E_STORE_FAILED"            # 记录目录写入失败

# ---- 沙箱 ----
E_SANDBOX_MISSING = "E_SANDBOX_MISSING"      # 沙箱还没准备或已被删除
E_SANDBOX_BROKEN = "E_SANDBOX_BROKEN"        # 沙箱完整性自检不过（junction/subst/.gitignore）
E_SNAPSHOT_FAILED = "E_SNAPSHOT_FAILED"      # 快照生成失败
E_LEAK_DETECTED = "E_LEAK_DETECTED"          # 快照里出现受测仓库绝对路径或敏感目录名
E_DRIVE_UNAVAILABLE = "E_DRIVE_UNAVAILABLE"  # 盘符池用尽

# ---- 校验 ----
E_GRADE_TIMEOUT = "E_GRADE_TIMEOUT"          # 子进程超时
E_GRADE_FAILED = "E_GRADE_FAILED"            # 校验执行失败（checkers 报错）
E_CHECK_TIMEOUT = "E_CHECK_TIMEOUT"

# ---- 环境 ----
E_REPO_UNREADABLE = "E_REPO_UNREADABLE"      # 受测仓库不可读
E_CONFIG_INVALID = "E_CONFIG_INVALID"        # config.json 缺项或类型错
E_INTERNAL = "E_INTERNAL"                    # 兜底：未预期异常

#: 各 code 的默认 HTTP 状态码（前端也可据此决定是否提示重试）
HTTP_STATUS = {
    OK: 200,
    E_BAD_REQUEST: 400,
    E_NOT_FOUND: 404,
    E_METHOD_NOT_ALLOWED: 405,
    E_TASK_NOT_FOUND: 404,
    E_TASK_INVALID: 500,
    E_MODEL_NOT_FOUND: 404,
    E_MODEL_INVALID: 400,
    E_CHAT_UNSUPPORTED: 400,
    E_CHAT_FAILED: 502,
    E_RUN_NOT_FOUND: 404,
    E_RUN_BUSY: 409,
    E_RUN_CANCELLED: 409,
    E_STORE_FAILED: 500,
    E_SANDBOX_MISSING: 409,
    E_SANDBOX_BROKEN: 409,
    E_SNAPSHOT_FAILED: 500,
    E_LEAK_DETECTED: 500,
    E_DRIVE_UNAVAILABLE: 503,
    E_GRADE_TIMEOUT: 504,
    E_GRADE_FAILED: 500,
    E_CHECK_TIMEOUT: 504,
    E_REPO_UNREADABLE: 503,
    E_CONFIG_INVALID: 500,
    E_INTERNAL: 500,
}


class HarnessError(Exception):
    """带稳定错误码的业务异常。

    :param code: 稳定错误码（见本模块常量）
    :param message: 面向用户的中文说明，结构为「发生了什么 + 影响 + 下一步」
    :param detail: 排障细节（默认折叠展示）
    """

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_dict(self) -> dict:
        """转成 API 响应体。"""
        body = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body

    def __repr__(self) -> str:  # pragma: no cover - 仅排障用
        return "HarnessError(%s, %r)" % (self.code, self.message)
