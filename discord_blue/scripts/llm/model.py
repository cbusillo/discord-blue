import logging
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

logger = logging.getLogger(__name__)


def generate_response(username: str, models_path: Path, message: str) -> str:
    model_path = models_path / username
    if not model_path.exists():
        return "Model not found"

    device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path).to(device)

    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    input_ids = tokenizer.encode(message, return_tensors="pt").to(device)
    attention_mask = input_ids != tokenizer.pad_token_id
    attention_mask = attention_mask.clone().detach().to(torch.long).to(device)

    output = model.generate(
        input_ids,
        attention_mask=attention_mask,
        max_length=100,
        num_return_sequences=1,
        do_sample=False,
        temperature=1.0,
        pad_token_id=tokenizer.pad_token_id,
        eos_token_id=tokenizer.eos_token_id,
    )

    generated_message = tokenizer.decode(output[0], skip_special_tokens=True)
    logger.info(f"Generated message: {generated_message}")
    return generated_message
