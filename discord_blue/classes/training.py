from pydantic import BaseModel, Field


class TrainingMessage(BaseModel):
    user_id: int
    username: str
    message: str
    message_id: int
    timestamp: float


class TrainingConversation(BaseModel):
    channel: str
    target: TrainingMessage
    context: list[TrainingMessage] = Field(default_factory=list)
    replied: TrainingMessage | None = None

    def add_context_message(self, message: TrainingMessage) -> None:
        self.context.insert(0, message)

    def set_replied_message(self, message: TrainingMessage) -> None:
        self.replied = message
