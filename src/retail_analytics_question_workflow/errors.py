"""领域错误类型。

应用通过命令行或服务层抛出这些错误；调用方可以据此区分输入错误、
权限不足、状态冲突和资料缺失，而不用解析字符串。
"""


class WorkflowError(Exception):
    """所有可预期业务错误的基类。"""


class ValidationError(WorkflowError):
    """输入或资料格式不合法。"""


class NotFoundError(WorkflowError):
    """引用的请求、分析或行动不存在。"""


class AuthorizationError(WorkflowError):
    """当前人员没有执行该操作的角色或地区权限。"""


class ConflictError(WorkflowError):
    """请求状态或唯一性约束不允许当前操作。"""


class PolicyError(WorkflowError):
    """数据访问决定与已登记的数据策略冲突。"""
