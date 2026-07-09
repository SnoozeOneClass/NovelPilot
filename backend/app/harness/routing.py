from typing import Literal

RoutingDecision = Literal[
    "continue",
    "revise",
    "rewrite",
    "commit",
    "pause",
    "escalate_to_arc",
    "escalate_to_book",
]

