import math
import os
import time
from typing import Any

import torch
import wandb

from megatron_lm.megatron.global_vars import get_args


def log_model_info(model: torch.nn.Module) -> None:
    model_config: dict[str, Any] = {}
    model_config["activation_function"] = model.config.hidden_act
    model_config["hidden_size"] = model.config.hidden_size
    model_config["model_type"] = model.config.model_type
    model_config["max_position_embeddings"] = model.config.max_position_embeddings
    model_config["num_attention_heads"] = model.config.num_attention_heads
    model_config["num_hidden_layers"] = model.config.num_hidden_layers
    model_config["model_architecture"] = model.config.architectures[0]

    print(f"model info: {model}")
    print(f"model config: {model.config}")
    wandb.config.update(model_config)

    # distributed training info
    world_size = int(os.environ["WORLD_SIZE"])
    wandb.config.update({"world_size": world_size})


def log_wandb(
    real_batch_size: int,
    real_seq_len: int,
    model: torch.nn.Module,
    accumulation_loss: float,
    load_balancing_loss: float,
    optimizer: torch.optim.Optimizer,
    iteration: int,
    gradient_accumulation_steps: int,
    world_size: int,
    iteration_start_time: float,
) -> None:
    wandb_stats: dict[str, Any] = {}

    # training info
    wandb_stats["training/loss"] = accumulation_loss
    wandb_stats["training/load_balancing_loss"] = load_balancing_loss
    wandb_stats["training/perplexity"] = math.exp(accumulation_loss)
    # utils info
    batch_size: int = real_batch_size
    sequence_length: int = real_seq_len

    wandb_stats["utils/batch_size"] = batch_size
    wandb_stats["utils/global_batch_size"] = batch_size * world_size * gradient_accumulation_steps
    wandb_stats["utils/seq_len"] = sequence_length
    wandb_stats["utils/gradient_accumulation_steps"] = gradient_accumulation_steps
    wandb_stats["utils/iteration"] = iteration

    # optimizer info
    wandb_stats["optimizer/lr"] = optimizer.param_groups[0]["lr"]

    optimizer_states_1: list[float] = [0.0] * 8
    optimizer_states_2: list[float] = [0.0] * 4

    for name, local_state in model.named_parameters():
        # docs: https://deepspeed.readthedocs.io/en/latest/zero3.html#debugging
        from deepspeed.utils import safe_get_local_fp32_param, safe_get_local_grad, safe_get_local_optimizer_state

        local_hp_param = safe_get_local_fp32_param(local_state)
        local_hp_grad = safe_get_local_grad(local_state)  # noqa: F841
        local_exp_avg = safe_get_local_optimizer_state(local_state, "exp_avg")
        local_exp_avg_sq = safe_get_local_optimizer_state(local_state, "exp_avg_sq")

        optimizer_states_1[0] += (torch.norm(local_exp_avg_sq).item()) ** 2  # type: ignore
        optimizer_states_1[1] += (torch.norm(local_exp_avg_sq.sqrt()).item()) ** 2  # type: ignore
        optimizer_states_1[2] += (torch.norm(local_exp_avg).item()) ** 2  # type: ignore
        optimizer_states_1[3] += (torch.norm(local_hp_param).item()) ** 2  # type: ignore
        optimizer_states_1[4] += torch.norm(local_exp_avg_sq, p=1).item()  # type: ignore
        optimizer_states_1[5] += torch.norm(local_exp_avg_sq.sqrt(), p=1).item()  # type: ignore
        optimizer_states_1[6] += torch.norm(local_exp_avg, p=1).item()  # type: ignore
        optimizer_states_1[7] += torch.norm(local_hp_param, p=1).item()
        optimizer_states_2[0] = max(
            optimizer_states_2[0],  # type: ignore
            abs(local_exp_avg_sq.max().item()),  # type: ignore
            abs(local_exp_avg_sq.min().item()),  # type: ignore
        )
        optimizer_states_2[1] = max(
            optimizer_states_2[1],
            local_exp_avg_sq.sqrt().abs_().max().item(),  # type: ignore
        )
        optimizer_states_2[2] = max(
            optimizer_states_2[2],  # type: ignore
            abs(local_exp_avg.max().item()),  # type: ignore
            abs(local_exp_avg.min().item()),  # type: ignore
        )
        optimizer_states_2[3] = max(
            optimizer_states_2[3],
            abs(local_hp_param.max().item()),  # type: ignore
            abs(local_hp_param.min().item()),  # type: ignore
        )
    if optimizer.state:  # optimizer stateがない場合はloggingしない
        # rank:0でしかoptimizer stateをloggingしないので world sizeで割る必要はない
        wandb_stats["optimizer/variance_l2"] = optimizer_states_1[0] ** 0.5
        wandb_stats["optimizer/variance_sqrt_l2"] = optimizer_states_1[1] ** 0.5
        wandb_stats["optimizer/momentum_l2"] = optimizer_states_1[2] ** 0.5
        wandb_stats["optimizer/weight_l2"] = optimizer_states_1[3] ** 0.5
        wandb_stats["optimizer/variance_l1"] = optimizer_states_1[4]
        wandb_stats["optimizer/variance_sqrt_l1"] = optimizer_states_1[5]
        wandb_stats["optimizer/momentum_l1"] = optimizer_states_1[6]
        wandb_stats["optimizer/weight_l1"] = optimizer_states_1[7]
        wandb_stats["optimizer/variance_abs_max"] = optimizer_states_2[0]
        wandb_stats["optimizer/variance_sqrt_abs_max"] = optimizer_states_2[1]
        wandb_stats["optimizer/momentum_abs_max"] = optimizer_states_2[2]
        wandb_stats["optimizer/weight_abs_max"] = optimizer_states_2[3]
        wandb_stats["utils/grad-norm"] = local_hp_grad.norm().item()  # type: ignore

    # stats
    iteration_elapsed_time = time.perf_counter() - iteration_start_time

    tokens_per_sec = batch_size * sequence_length * gradient_accumulation_steps / iteration_elapsed_time * world_size
    wandb_stats["stats/1_iteration_time"] = iteration_elapsed_time
    wandb_stats["stats/tokens_per_sec"] = tokens_per_sec
    wandb_stats["stats/tokens_per_sec_per_gpu"] = tokens_per_sec / world_size

    num_layers: int = model.config.num_hidden_layers
    hidden_size: int = model.config.hidden_size
    vocab_size: int = model.config.vocab_size
    activation_func: str = model.config.hidden_act
    intermediate_size: int = model.config.intermediate_size
    num_experts_routed_to: int = 1
    if hasattr(model.config, "num_experts_per_tok"):
        num_experts_routed_to = model.config.num_experts_per_tok

    activation_function_factor: float = 1  # GELU
    if activation_func == "silu":
        activation_function_factor = 1 + 0.5  # SWiGLU (upscaling + down scaling)
    num_attention_heads: int = model.config.num_attention_heads

    batch_size = batch_size * gradient_accumulation_steps
    kv_channels = hidden_size // num_attention_heads
    query_projection_size = kv_channels * num_attention_heads
    query_projection_to_hidden_size_ratio = query_projection_size / hidden_size

    # tflops calculation
    flops_per_iteration: float = (
        12
        * batch_size
        * sequence_length
        * (hidden_size**2)
        * num_layers
        * (
            (
                # Attention
                (1 + (model.config.num_key_value_heads / num_attention_heads) + (sequence_length / hidden_size))
                * query_projection_to_hidden_size_ratio
            )
            # MLP
            + ((intermediate_size / hidden_size) * num_experts_routed_to * activation_function_factor)
            # Logit
            + (vocab_size / (2 * num_layers * hidden_size))
        )
    )
    tflops: float = flops_per_iteration / (iteration_elapsed_time * (10**12))
    wandb_stats["stats/tflops"] = tflops

    wandb.log(wandb_stats, step=iteration)

    print("------------------------------------------------------------------")
    print(
        f"iteration: {iteration} , TFLOPS: {tflops}, Tokens per sec: {tokens_per_sec}, Loss: {accumulation_loss}, load balancing loss: {load_balancing_loss}"
    )
    print(
        "------------------------------------------------------------------",
        flush=True,
    )


def update_iter_info() -> None:
    args = get_args()

    print(
        f"\ntrain_iters: {args.train_iters}, lr_decay_iters: {args.lr_decay_iters}, lr_warmup_iters: {args.lr_warmup_iters}\n",
        flush=True,
    )

    wandb.config.update(
        {
            "train_iters": args.train_iters,
            "lr_decay_iters": args.lr_decay_iters,
            "lr_warmup_iters": args.lr_warmup_iters,
            "instruction_dataset_size": args.instruction_dataset_size,
            "save_sampler_state": args.save_sampler_state,
        },
        allow_val_change=True,
    )
