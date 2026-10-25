TEST_CASES = [
    {"input": "订单DD20260901001物流到哪了", "expect_type": "query_logistics", "expect_auto": True},
    {"input": "我要退款，订单DD20260905002", "expect_type": "refund", "expect_risk": "medium"},
    {"input": "东西坏了，怎么理赔", "expect_missing": ["order_no"], "expect_ask": True},
]
