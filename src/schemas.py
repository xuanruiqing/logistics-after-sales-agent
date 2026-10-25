from typing import Optional, List
from pydantic import BaseModel, Field, field_validator

# 全项目唯一的一套诉求分类，router / 抽取 / 风控 / 评测都引用这里
REQUEST_TYPES = [
    "query_logistics",
    "refund",
    "exchange",
    "damage_claim",
    "complaint",
    "other",
]


class ExtractResult(BaseModel):
    """信息抽取 Agent 的结构化输出"""
    request_type: str = Field(description="诉求类型，取值见 REQUEST_TYPES")
    order_no: Optional[str] = Field(None, description="订单号，DD 开头")
    product_id: Optional[str] = Field(None, description="商品编号")
    problem_desc: str = Field(default="", description="用户问题原文")
    missing_fields: List[str] = Field(default_factory=list, description="缺失的必填字段")

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _none_to_empty(cls, value):
        # 模型有时会输出 null，统一转成空列表，避免整条抽取结果校验失败
        return value or []


class PolicyDecision(BaseModel):
    """规则风控决策输出"""
    risk_level: str = Field(description="风险等级：low / medium / high")
    allow_auto: bool = Field(description="是否允许 AI 自动处理")
    need_human: bool = Field(description="是否需要人工介入")
    reason: str = Field(description="决策理由")


class TicketSchema(BaseModel):
    """售后工单结构"""
    ticket_id: str
    session_id: str
    order_no: Optional[str] = None
    request_type: str
    problem_desc: str
    status: str  # ai_solved / pending_human / closed
    handle_result: str
    create_time: str
    amount: Optional[float] = None


class ChatRequest(BaseModel):
    session_id: str
    user_input: str


class ChatResponse(BaseModel):
    reply: str
    ticket_id: Optional[str] = None
    status: str
    need_human: bool = False


class ReviewRequest(BaseModel):
    """人工复核工单"""
    approved: bool
    comment: str = ""
