from dataclasses import dataclass

from app.llm.gateway import ChatChunk


@dataclass
class StreamProgressAccumulator:
    """Coalesce provider fragments into bounded, safe progress checkpoints."""

    minimum_emit_delta: int = 512
    received_characters: int = 0
    last_emitted_characters: int = 0

    def observe(self, chunk: ChatChunk) -> int | None:
        delta = chunk.text_delta or chunk.arguments_delta
        if delta:
            self.received_characters += len(delta)
        completed = chunk.event_type in {"tool_call_stop", "message_stop"}
        if self.received_characters == 0:
            return None
        should_emit = (
            self.last_emitted_characters == 0
            or self.received_characters - self.last_emitted_characters
            >= self.minimum_emit_delta
            or (
                completed
                and self.received_characters != self.last_emitted_characters
            )
        )
        if not should_emit:
            return None
        self.last_emitted_characters = self.received_characters
        return self.received_characters
