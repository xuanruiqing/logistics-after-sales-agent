from src.config import config
from src.schemas import PolicyDecision


class PolicyEngine:
    """确定性风控规则，与 data/shouhouguize.txt 中的退款阈值保持一致。

    LLM 不参与金额判定，避免模型幻觉导致错误放款。
    """

    @staticmethod
    def evaluate(request_type: str, order_amount: float = 0, user_id: str = "") -> PolicyDecision:
        # 规则 1：退款按订单金额分档
        if request_type == "refund":
            if order_amount <= config.AUTO_REFUND_MAX:
                return PolicyDecision(
                    risk_level="low", allow_auto=True, need_human=False,
                    reason=f"订单金额 {order_amount} 元 ≤ {config.AUTO_REFUND_MAX} 元，符合自动退款阈值",
                )
            if order_amount <= config.APPROVAL_REFUND_MAX:
                return PolicyDecision(
                    risk_level="medium", allow_auto=False, need_human=True,
                    reason=f"订单金额 {order_amount} 元，处于人工审核区间",
                )
            return PolicyDecision(
                risk_level="high", allow_auto=False, need_human=True,
                reason=f"订单金额 {order_amount} 元 > {config.APPROVAL_REFUND_MAX} 元，需主管审批",
            )

        # 规则 2：查询类一律低风险自动处理
        if request_type in ("query_logistics", "other"):
            return PolicyDecision(
                risk_level="low", allow_auto=True, need_human=False,
                reason="信息查询类，自动处理",
            )

        # 规则 3：理赔需核验凭证
        if request_type == "damage_claim":
            return PolicyDecision(
                risk_level="medium", allow_auto=False, need_human=True,
                reason="破损理赔需人工核验照片与面单凭证",
            )

        # 规则 4：换货需核验商品状态与签收时间
        if request_type == "exchange":
            return PolicyDecision(
                risk_level="medium", allow_auto=False, need_human=True,
                reason="换货需核验商品状态及是否在 7 天无理由期限内",
            )

        # 规则 5：投诉类必须人工介入
        if request_type == "complaint":
            return PolicyDecision(
                risk_level="high", allow_auto=False, need_human=True,
                reason="投诉/纠纷类需人工客服跟进",
            )

        return PolicyDecision(
            risk_level="low", allow_auto=True, need_human=False,
            reason="默认低风险",
        )


policy_engine = PolicyEngine()
