"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .normalize import IssueCode, IssueSeverity


class EntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class PreflightEntryIn(BaseModel):
    path: str = Field(..., description="待解包条目的原始路径")
    type: EntryType = Field(..., description="条目类型：file、directory 或 symlink")
    link_target: str | None = Field(
        default=None,
        description="符号链接目标（type 为 symlink 时必填；file/directory 不得携带）",
    )
    compressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "声明的压缩后字节数（非负整数，仅 file 条目可携带；"
            "directory/symlink 携带返回 422）"
        ),
    )
    uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "声明的解压后字节数（非负整数，仅 file 条目可携带；"
            "directory/symlink 携带返回 422）"
        ),
    )

    @model_validator(mode="after")
    def _validate_fields(self) -> "PreflightEntryIn":
        if self.type is EntryType.SYMLINK:
            if self.link_target is None:
                raise ValueError("type 为 symlink 时必须提供 link_target")
        elif self.link_target is not None:
            raise ValueError("仅 symlink 条目可携带 link_target")
        if self.type is not EntryType.FILE:
            if self.compressed_bytes is not None:
                raise ValueError("仅 file 条目可携带 compressed_bytes")
            if self.uncompressed_bytes is not None:
                raise ValueError("仅 file 条目可携带 uncompressed_bytes")
        return self


class ResourceLimits(BaseModel):
    """可选的解压资源预算限制；各字段省略表示不启用对应限制。"""

    max_uncompressed_bytes_per_file: int | None = Field(
        default=None,
        ge=0,
        description=(
            "单文件解压大小上限（非负整数）；启用时每个 file 条目必须声明 "
            "uncompressed_bytes，否则返回 422"
        ),
    )
    max_uncompressed_bytes_total: int | None = Field(
        default=None,
        ge=0,
        description=(
            "整批文件声明解压总量上限（非负整数）；启用时每个 file 条目必须声明 "
            "uncompressed_bytes，否则返回 422"
        ),
    )
    max_compression_ratio: int | None = Field(
        default=None,
        ge=1,
        description=(
            "最大压缩比（正整数 N，表示 N:1）；以整数交叉乘法判定，启用时每个 "
            "file 条目必须同时声明 compressed_bytes 与 uncompressed_bytes，否则 "
            "返回 422。compressed_bytes 为 0 且 uncompressed_bytes 大于 0 视为超限；"
            "二者均为 0 时通过"
        ),
    )


class PreflightRequest(BaseModel):
    entries: list[PreflightEntryIn] = Field(
        ..., min_length=1, description="待预检的条目列表（至少 1 条）"
    )
    repair: bool = Field(
        default=False,
        description="为 true 时生成确定性 repair_plan（同名碰撞与文件祖先冲突修复建议）",
    )
    resource_limits: ResourceLimits | None = Field(
        default=None,
        description=(
            "可选的解压资源预算限制（单文件解压大小、整批解压总量、最大压缩比）；"
            "省略或为空对象时不启用任何限制，响应保持兼容"
        ),
    )

    @model_validator(mode="after")
    def _validate_resource_limits_declarations(self) -> "PreflightRequest":
        """启用某限制时，校验每个文件条目都声明了该限制所需的大小字段。"""
        limits = self.resource_limits
        if limits is None or not limits.model_fields_set:
            return self

        need_uncompressed = (
            limits.max_uncompressed_bytes_per_file is not None
            or limits.max_uncompressed_bytes_total is not None
            or limits.max_compression_ratio is not None
        )
        need_compressed = limits.max_compression_ratio is not None

        missing: list[str] = []
        for i, entry in enumerate(self.entries, start=1):
            if entry.type is not EntryType.FILE:
                continue
            if need_uncompressed and entry.uncompressed_bytes is None:
                missing.append(f"#{i} 缺少 uncompressed_bytes")
            if need_compressed and entry.compressed_bytes is None:
                missing.append(f"#{i} 缺少 compressed_bytes")
        if missing:
            raise ValueError(
                "启用 resource_limits 时每个 file 条目必须声明所需大小字段："
                + "；".join(missing)
            )
        return self


class IssueOut(BaseModel):
    code: IssueCode
    severity: IssueSeverity
    message: str
    segment: int | None = None
    related_index: int | None = None


class CollisionMember(BaseModel):
    index: int
    canonical_path: str


class CollisionGroup(BaseModel):
    group: int = Field(..., description="碰撞组编号（按首次出现顺序分配）")
    canonical_path: str = Field(..., description="该组对应的规范路径（展示用）")
    members: list[CollisionMember]


class HierarchyConflict(BaseModel):
    file_index: int = Field(..., description="作为祖先路径的文件条目下标")
    file_canonical_path: str
    descendant_index: int = Field(..., description="被阻挡的后代条目下标")
    descendant_canonical_path: str


class PreflightEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    type: EntryType
    original_path: str
    link_target: str | None = Field(
        default=None, description="符号链接目标（仅 symlink 条目有值）"
    )
    compressed_bytes: int | None = Field(
        default=None, description="声明的压缩后字节数（仅 file 条目可携带）"
    )
    uncompressed_bytes: int | None = Field(
        default=None, description="声明的解压后字节数（仅 file 条目可携带）"
    )
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool
    collision_group: int | None = Field(
        ..., description="所属同名碰撞组编号；无碰撞或不安全时为 null"
    )
    blocked_by_file_ancestors: list[int] = Field(
        default_factory=list, description="阻挡本条目的文件祖先下标列表"
    )
    blocks_descendants: list[int] = Field(
        default_factory=list, description="被本文件条目阻挡的后代下标列表（仅文件条目）"
    )
    issues: list[IssueOut] = Field(default_factory=list)


class SymlinkChainStep(BaseModel):
    index: int | None = Field(
        ..., description="该步对应的符号链接条目下标；解析终点（非链接路径）为 null"
    )
    path: str = Field(..., description="该步的规范路径（正斜杠分隔）")


class SymlinkLinkOut(BaseModel):
    index: int = Field(..., description="符号链接条目下标（与输入顺序一致）")
    original_path: str
    link_target: str = Field(..., description="原始链接目标")
    resolved_target: str | None = Field(
        ...,
        description="自身目标相对链接所在目录解析后的根内路径；越界为 null",
    )
    final_path: str | None = Field(
        ...,
        description="沿链接链解析到的最终根内路径；目标越界或链存在循环时为 null",
    )
    chain: list[SymlinkChainStep] = Field(
        ...,
        description="解析链：依次为自身、途经的符号链接，以及最终解析终点（若有）",
    )
    risk_codes: list[IssueCode] = Field(
        default_factory=list,
        description=(
            "链接风险代码：LINK_TARGET_ESCAPE（目标或链越出目标根）、"
            "LINK_TARGET_INVALID（目标非法）、LINK_CYCLE（链存在循环）"
        ),
    )
    safe: bool = Field(..., description="无任何 error 级风险时为 true")


class SymlinkCycleOut(BaseModel):
    members: list[int] = Field(
        ..., description="构成循环的符号链接下标（按链顺序，从最小下标开始）"
    )


class SymlinkHijackOut(BaseModel):
    entry_index: int = Field(..., description="被劫持的文件/目录条目下标")
    canonical_path: str
    symlink_ancestors: list[int] = Field(
        ..., description="占据该条目祖先路径的符号链接下标（由近及远按深度升序）"
    )


class SymlinkAnalysisOut(BaseModel):
    links: list[SymlinkLinkOut] = Field(
        ..., description="逐链接解析链与风险代码（按输入顺序）"
    )
    cycles: list[SymlinkCycleOut] = Field(..., description="检测到的链接循环组")
    hijacked_entries: list[SymlinkHijackOut] = Field(
        ..., description="祖先路径被符号链接占据的文件/目录条目"
    )


class Summary(BaseModel):
    total: int
    safe: int
    unsafe: int
    collisions: int = Field(..., description="同名碰撞组数量")
    collision_entries: int
    hierarchy_conflicts: int
    symlinks: int = Field(..., description="符号链接条目数量")
    symlink_cycles: int = Field(..., description="检测到的链接循环组数")
    symlink_hijacked: int = Field(
        ..., description="被符号链接祖先劫持的文件/目录条目数量"
    )
    total_compressed_bytes: int = Field(
        default=0,
        description=(
            "整批文件声明的压缩后字节总量（仅累加声明了 compressed_bytes 的 "
            "file 条目；未声明时为 0）"
        ),
    )
    total_uncompressed_bytes: int = Field(
        default=0,
        description=(
            "整批文件声明的解压后字节总量（仅累加声明了 uncompressed_bytes 的 "
            "file 条目；未声明时为 0）"
        ),
    )
    resource_limits_exceeded: bool = Field(
        default=False,
        description=(
            "批次资源预算超限状态：任一文件命中单文件解压大小或最大压缩比限制，"
            "或整批声明解压总量超过上限时为 true；未启用 resource_limits 时为 false"
        ),
    )


class RepairReason(str, Enum):
    COLLISION = "COLLISION"
    BLOCKS_DESCENDANT = "BLOCKS_DESCENDANT"
    UNSAFE = "UNSAFE"
    SYMLINK = "SYMLINK"


class RepairEntry(BaseModel):
    index: int = Field(..., description="条目下标（与输入顺序一致，从 1 开始）")
    original_path: str = Field(..., description="原始输入路径")
    repaired_path: str | None = Field(
        ...,
        description=(
            "修复后路径（正斜杠分隔）。安全且未改名的条目等于 canonical_path；"
            "skipped 条目为 null"
        ),
    )
    reasons: list[RepairReason] = Field(
        default_factory=list,
        description=(
            "修复原因：COLLISION（同名碰撞改让）、"
            "BLOCKS_DESCENDANT（文件阻挡后代而改名）；skipped 条目为 "
            "[UNSAFE]（不安全，含被符号链接祖先劫持）或 [SYMLINK]（符号链接）"
        ),
    )
    skipped: bool = Field(
        ..., description="为 true 表示不参与改名与复检（不安全条目与全部符号链接）"
    )


class RepairPlan(BaseModel):
    entries: list[RepairEntry] = Field(
        ..., description="按输入顺序排列的逐条修复计划"
    )
    renamed_count: int = Field(..., description="实际改名的安全条目数量")
    skipped_count: int = Field(
        ..., description="被跳过的条目数量（不安全条目与全部符号链接）"
    )
    collisions_remaining: int = Field(
        ..., description="按 repaired_path 复检后的同名碰撞组数（恒为 0）"
    )
    hierarchy_conflicts_remaining: int = Field(
        ..., description="按 repaired_path 复检后的文件祖先冲突对数（恒为 0）"
    )
    collisions_resolved: int = Field(
        ..., description="修复消除的同名碰撞组数（等于原 summary.collisions）"
    )
    hierarchy_conflicts_resolved: int = Field(
        ..., description="修复消除的文件祖先冲突对数（等于原 summary.hierarchy_conflicts）"
    )


class PreflightResponse(BaseModel):
    target: str = "windows"
    entries: list[PreflightEntryOut]
    collision_groups: list[CollisionGroup]
    hierarchy_conflicts: list[HierarchyConflict]
    symlink_analysis: SymlinkAnalysisOut
    summary: Summary


class PreflightRepairResponse(PreflightResponse):
    repair_plan: RepairPlan


class HealthResponse(BaseModel):
    status: str
    service: str
