import uuid
from src.db.database import create_ticket
from src.utils import current_time_str


class TicketService:
    """售后工单落库"""

    @staticmethod
    def create(session_id: str, order_no: str, request_type: str, problem_desc: str,
               status: str, handle_result: str, amount: float = None, trace_id: str = "") -> dict:
        ticket = {
            "ticket_id": f"TK{uuid.uuid4().hex[:10].upper()}",
            "session_id": session_id,
            "order_no": order_no,
            "request_type": request_type,
            "problem_desc": problem_desc,
            "status": status,
            "handle_result": handle_result,
            "amount": amount,
            "trace_id": trace_id,
            "create_time": current_time_str(),
        }
        create_ticket(ticket)
        return ticket
