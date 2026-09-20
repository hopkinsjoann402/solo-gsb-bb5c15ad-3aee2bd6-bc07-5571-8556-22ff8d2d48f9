"""解压资源预算校验（纯内存、不访问文件系统）。

按请求声明的 ``compressed_bytes`` / ``uncompressed_bytes`` 校验可选的
``resource_limits``：

* ``max_uncompressed_bytes_per_file``：单文件解压大小上限；
* ``max_uncompressed_bytes_total``：整批文件声明解压总量上限；
* ``max_compression_ratio``：最大压缩比（正整数）。

压缩比判定**只使用整数交叉乘法**（``uncompressed > ratio * compressed``），
不引入任何浮点除法：

* ``compressed = 0`` 且 ``uncompressed > 0`` 时右侧乘积为 0，不等式成立 → 超限；
* 二者均为 0 时 ``0 > 0`` 不成立 → 通过。

单文件超限分别追加稳定问题码 ``UNCOMPRESSED_SIZE_EXCEEDED`` 与
``COMPRESSION_RATIO_EXCEEDED``（error 级，超限文件随之 unsafe）；
整批总量是否超限由汇总结果 ``total_uncompressed_exceeded`` 表示。
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import Issue, IssueCode, IssueSeverity, NormalizedEntry
from .schemas import PreflightRequest, ResourceLimits


@dataclass(frozen=True)
class ResourceBudgetResult:
    """资源预算校验结果。"""

    total_compressed_bytes: int = 0
    total_uncompressed_bytes: int = 0
    total_uncompressed_exceeded: bool = False


def evaluate_resource_budget(
    request: PreflightRequest,
    normalized: list[NormalizedEntry],
) -> ResourceBudgetResult:
    """校验整批条目的解压资源预算。

    副作用：向超限的文件条目追加 error 级问题（该问题使条目 unsafe，
    从而退出后续碰撞分组、层级冲突与 repair 复检）。

    汇总字节只统计**声明了对应字段的文件条目**；目录与符号链接无大小语义，
    不参与求和（携带大小字段在请求校验阶段已被拒绝，返回 422）。
    """
    limits: ResourceLimits | None = request.resource_limits
    by_index = {
        i: e for i, e in enumerate(request.entries, start=1)
    }

    total_compressed = 0
    total_uncompressed = 0
    for entry in normalized:
        src = by_index[entry.index]
        if src.type.value != "file":
            continue
        if src.compressed_bytes is not None:
            total_compressed += src.compressed_bytes
        if src.uncompressed_bytes is not None:
            total_uncompressed += src.uncompressed_bytes

    # 无 resource_limits、或传入空对象（{}）时不启用任何限制。
    if limits is None or not limits.model_fields_set:
        return ResourceBudgetResult(
            total_compressed_bytes=total_compressed,
            total_uncompressed_bytes=total_uncompressed,
        )

    max_per_file = limits.max_uncompressed_bytes_per_file
    max_total = limits.max_uncompressed_bytes_total
    max_ratio = limits.max_compression_ratio

    for entry in normalized:
        src = by_index[entry.index]
        if src.type.value != "file":
            continue

        uncompressed = src.uncompressed_bytes
        compressed = src.compressed_bytes

        if max_per_file is not None and uncompressed is not None:
            if uncompressed > max_per_file:
                entry.issues.append(
                    Issue(
                        IssueCode.UNCOMPRESSED_SIZE_EXCEEDED,
                        IssueSeverity.ERROR,
                        f"声明解压大小 {uncompressed} 字节超过单文件上限 "
                        f"{max_per_file} 字节",
                    )
                )

        if max_ratio is not None:
            # 整数交叉乘法，禁止浮点除法；compressed=0、uncompressed>0 时超限，
            # 二者均为 0 时 0 > 0 不成立，通过。
            if uncompressed is not None and compressed is not None:
                if uncompressed > max_ratio * compressed:
                    entry.issues.append(
                        Issue(
                            IssueCode.COMPRESSION_RATIO_EXCEEDED,
                            IssueSeverity.ERROR,
                            f"声明压缩比超过上限 {max_ratio}:1：压缩 "
                            f"{compressed} 字节、解压 {uncompressed} 字节，"
                            f"整数交叉乘法判定 {uncompressed} > "
                            f"{max_ratio} * {compressed}",
                        )
                    )

    total_exceeded = (
        max_total is not None and total_uncompressed > max_total
    )

    return ResourceBudgetResult(
        total_compressed_bytes=total_compressed,
        total_uncompressed_bytes=total_uncompressed,
        total_uncompressed_exceeded=total_exceeded,
    )
