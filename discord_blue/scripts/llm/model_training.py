import gc
import json
import logging
import shutil
from pathlib import Path

import polars as pl
import torch
from huggingface_hub import login
from torch.utils.data import Dataset as TorchDataset
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedTokenizerBase, IntervalStrategy
from transformers.trainer import Trainer
from transformers.trainer_callback import EarlyStoppingCallback
from transformers.training_args import TrainingArguments

from discord_blue.classes.training import TrainingConversation
from discord_blue.config import config

logger = logging.getLogger(__name__)


def load_conversations(file_path: Path) -> list[TrainingConversation]:
    training_conversations = []
    file_content = file_path.read_text().strip()
    if file_path.suffix == ".jsonl":
        for line in file_content.splitlines():
            training_conversations.append(TrainingConversation.model_validate_json(line))
    elif file_path.suffix == ".json":
        conversations = json.loads(file_content)
        for conversation in conversations:
            training_conversations.append(TrainingConversation.model_validate(conversation))

    return training_conversations


def train_models(username: str, input_path: Path, model_name: str) -> None:
    logger.info(f"Training models for {username} in {input_path}")
    if username == "all":
        file_paths = input_path.glob("*.json*")
        usernames = set()
        for file_path in file_paths:
            usernames.add(file_path.stem.split("_")[0])
        for username in usernames:
            logger.info(f"Training models for {username}")
            train_model(list(input_path.glob(f"{username}*.json*")), model_name)
    else:
        train_model(list(input_path.glob(f"{username}*.json*")), model_name)


def train_model(file_paths: list[Path], model_name: str) -> None:
    training_data = [conversation for training_file in file_paths for conversation in load_conversations(training_file)]

    if "llama-3.1" in model_name.lower():
        login(config.hugging_face.token)

    device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False, clean_up_tokenization_spaces=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        use_cache=False,
    )
    model.gradient_checkpointing_enable()
    model.to(device)

    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    dataframe = generate_dataset(training_data)
    dataset = ConversationDataset(dataframe, tokenizer)

    output_dir = file_paths[0].parent / "fine_tuned_models" / training_data[0].target.username
    training_args = TrainingArguments(
        output_dir=output_dir.as_posix(),
        overwrite_output_dir=True,
        num_train_epochs=5,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        save_steps=500,
        prediction_loss_only=True,
        bf16=True,
        bf16_full_eval=True,
        logging_steps=500,
        evaluation_strategy=IntervalStrategy.NO,
        dataloader_num_workers=2,
        dataloader_pin_memory=True,
        load_best_model_at_end=True,
        eval_steps=500,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=2)],
    )

    trainer.train()
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    del model
    gc.collect()
    torch.mps.empty_cache()
    checkpoint_paths = output_dir.glob("checkpoint-*")
    for checkpoint_path in checkpoint_paths:
        shutil.rmtree(checkpoint_path)
    logger.info(f"Model trained and saved to {output_dir}")


def generate_dataset(training_data: list[TrainingConversation]) -> pl.DataFrame:
    logger.info(f"Generating dataset with {len(training_data)} conversations for user {training_data[0].target.username}")
    records: list[dict[str, str]] = []

    for conversation in training_data:
        context_text = "\n".join(f"{message.username}: {message.message}" for message in conversation.context)
        if conversation.replied:
            context_text += f"\n{conversation.replied.username}: {conversation.replied.message}"
        records.append({"input": context_text, "output": conversation.target.message})

    return pl.DataFrame(records)


class ConversationDataset(TorchDataset):
    def __init__(self, dataframe: pl.DataFrame, tokenizer: PreTrainedTokenizerBase) -> None:
        pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id
        self._examples = []
        for row in dataframe.iter_rows(named=True):
            encoded = tokenizer(
                row["input"],
                padding="max_length",
                truncation=True,
                max_length=512,
            )
            labels = [token if token != pad_id else -100 for token in encoded["input_ids"]]
            self._examples.append(
                {
                    "input_ids": torch.tensor(encoded["input_ids"]),
                    "attention_mask": torch.tensor(encoded["attention_mask"]),
                    "labels": torch.tensor(labels),
                }
            )

    def __len__(self) -> int:
        return len(self._examples)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return self._examples[index]
