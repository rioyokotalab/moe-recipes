import os
import sys

import torch
import torch.distributed as torch_distributed
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR
from deepspeed.utils import set_z3_leaf_modules  # mixtral
from transformers.models.mixtral.modeling_mixtral import MixtralSparseMoeBlock
import deepspeed
from accelerate import Accelerator
from accelerate.utils import DeepSpeedPlugin
import wandb

from llama_recipes.utils.train_utils import (
    clear_gpu_cache,
    print_model_size,
    setup_environ_flags,
    train,
)
from llama_recipes.optimizer import WarmupCosineAnnealingLR
from llama_recipes.utils.random import set_seed
from llama_recipes.utils.distributed import (
    print_rank_0,
    is_rank_0,
    set_mpi_env,
    get_rank,
    get_local_rank,
)
from llama_recipes.get_models import get_model
from llama_recipes.utils.checkpoint import (
    load_model_state_dict,
    load_optimizer_state_dict,
    load_scheduler_state_dict,
    load_rng_state_dict,
    get_latest_iteration,
)

from llama_recipes.arguments import parse_args
from llama_recipes.get_fsdp import get_sharding_strategy
from megatron_lm.megatron.global_vars import set_global_variables


current_path: str = os.getcwd()
sys.path.append(f"{current_path}/llama-recipes/src/")


def main() -> None:
    # initialize
    args = parse_args()
    set_global_variables(args=args)

    # Set the seeds for reproducibility
    set_seed(seed=args.seed)

    # Distributed args.
    if args.use_mpi:
        set_mpi_env()

    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    args.rank = rank
    args.world_size = world_size
    args.gradient_accumulation_steps = args.global_batch_size // (args.micro_batch_size * world_size)

    # torch_distributed.init_process_group(backend="nccl", world_size=world_size, rank=rank)
    deepPlugin = DeepSpeedPlugin(
        hf_ds_config='scripts/abci/mixtral/mixtral-config.json',
        zero3_init_flag=True
    )
    accelerator = Accelerator(
        mixed_precision='bf16',
        deepspeed_plugin=deepPlugin,
        gradient_accumulation_steps=args.gradient_accumulation_steps
    )

    # wandb setting
    if args.wandb_name is not None and is_rank_0():
        import datetime

        now = datetime.datetime.now()
        now = now.strftime("%Y-%m-%d-%H-%M-%S")
        wandb_setting: dict = {
            "entity": args.wandb_entity,
            "project": args.wandb_project,
            "name": args.wandb_name,
            "config": vars(args),
        }
        wandb.init(**wandb_setting)

    if torch_distributed.is_initialized():
        torch.cuda.set_device(get_local_rank())  # type: ignore
        clear_gpu_cache(get_local_rank())  # type: ignore
        setup_environ_flags(get_rank())  # type: ignore

    iteration: int = get_latest_iteration(args.load)
    args.iteration = iteration
    torch.distributed.barrier()

    # random seed
    if args.load:
        load_rng_state_dict(args.load)
        torch_distributed.barrier()

    # dataset
    from llama_recipes.datasets.pretrain_dataset import build_train_valid_test_datasets
    from megatron_lm.megatron.data.data_samplers import build_pretraining_data_loader

    train_dataset, validation_dataset, test_dataset = build_train_valid_test_datasets()

    args.consumed_train_samples = args.global_batch_size * args.iteration
    args.consumed_valid_samples = args.global_batch_size * (
        args.iteration // args.eval_interval) * args.eval_iters

    train_dataloader = build_pretraining_data_loader(
        dataset=train_dataset,
        consumed_samples=args.consumed_train_samples,
    )
    validation_dataloader = build_pretraining_data_loader(
        dataset=validation_dataset,
        consumed_samples=args.consumed_valid_samples,
    )
    torch_distributed.barrier()

    use_cache = False
    model = get_model(
        model_name=args.base_model, use_cache=use_cache
    )
    model.gradient_checkpointing_enable(  # type: ignore
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    model.enable_input_require_grads()  # type: ignore
    print_rank_0("Gradient checkpointing enable")

    if args.load:
        load_model_state_dict(model, args.load)  # type: ignore

    print_model_size(model, args.base_model, rank)  # type: ignore

    # Convert the model to bfloat16 if fsdp and pure_bf16 is enabled
    if args.bf16:
        model.to(torch.bfloat16)  # type: ignore
    elif args.fp16:
        model.to(torch.float16)  # type: ignore

    set_z3_leaf_modules(  # z3_leaf
        model=model, leaf_module_classes=[MixtralSparseMoeBlock]  # type: ignore
    )
    model.train()  # type: ignore

    optimizer = optim.AdamW(
        model.parameters(),  # type: ignore
        lr=args.lr,
        betas=(args.adam_beta1, args.adam_beta2),
        eps=args.adam_eps,
        weight_decay=args.weight_decay,
    )

    if args.load:
        load_optimizer_state_dict(model=model, optimizer=optimizer, path=args.load)  # type: ignore

    if args.lr_decay_style == "cosine":
        scheduler = WarmupCosineAnnealingLR(
            optimizer=optimizer,
            warmup_iterations=args.lr_warmup_iters,
            decay_iterations=args.lr_decay_iters,
            max_iterations=args.train_iters,
            eta_min=args.min_lr,
        )
    else:
        scheduler = StepLR(optimizer, step_size=1, gamma=0.85)

    if args.load:
        load_scheduler_state_dict(scheduler, args.load)  # type: ignore

    # deepspeed_config: dict = {
    #     "bf16": {
    #         "enabled": True
    #     },
    #     "zero_optimization": {
    #         "stage": 3,
    #         "overlap_comm": True,
    #         "contiguous_gradients": True,
    #         "sub_group_size": 1e9,
    #         "reduce_bucket_size": "auto",
    #         "stage3_prefetch_bucket_size": 0,
    #         "stage3_param_persistence_threshold": "auto",
    #         "stage3_max_live_parameters": 1e9,
    #         "stage3_max_reuse_distance": 1e9,
    #         "stage3_gather_16bit_weights_on_model_save": True
    #     },
    #     "train_micro_batch_size_per_gpu": args.micro_batch_size,
    #     "offload_param_device": "cpu"
    # }

    # model, optimizer, _, scheduler = deepspeed.initialize(
    #     model=model,  # type: ignore
    #     optimizer=optimizer,
    #     lr_scheduler=scheduler,  # type: ignore
    #     config=deepspeed_config,
    # )
    model, optimizer, train_dataloader, scheduler = accelerator.prepare(
        model,
        optimizer,
        train_dataloader,
        scheduler
    )

    # Start the training process
    train(
        model=model,
        train_dataloader=train_dataloader,
        eval_dataloader=validation_dataloader,
        optimizer=optimizer,  # type: ignore
        lr_scheduler=scheduler,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        local_rank=get_local_rank(),
        rank=get_rank(),
    )


if __name__ == "__main__":
    main()
