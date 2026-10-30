# 新建文件：src/faq/faq_matcher.py
from .faq_data import FAQ_LIST
import re

def faq_match(user_input: str):
    """
    FAQ匹配：命中返回对应答案，未命中返回None
    """
    # 预处理：去掉标点、空格，转小写
    text = re.sub(r'[^\w\u4e00-\u9fa5]', '', user_input).lower()
    
    # 带订单号的一律不短路，必须走Agent
    if re.search(r'DD\d+|订单\d+', user_input):
        return None
    
    for item in FAQ_LIST:
        for kw in item["keywords"]:
            if kw in text:
                return item["answer"]
    return None
