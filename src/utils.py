import logging
import uuid
from datetime import datetime

def init_logger(name="logistics_agent"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger

logger = init_logger()

def gen_trace_id():
    return f"trace_{uuid.uuid4().hex[:12]}"

def current_time_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
