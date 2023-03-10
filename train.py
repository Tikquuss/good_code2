import os
from argparse import ArgumentParser, Namespace

import wandb

import torch
import pytorch_lightning as pl
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import Callback, ModelCheckpoint, EarlyStopping
from pytorch_lightning.loggers import CSVLogger, TensorBoardLogger

from src.utils import bool_flag, str2dic_all, init_wandb, AttrDict, intorstr, to_none
from src.data import DEFAULT_DATA_DIR
from src.dataset import DataModule
from src.modeling import TrainableTransformer, FALSE_SAVE_DEBUGGING
#FALSE_SAVE_DEBUGGING=False

def get_parser():
    """
    Generate a parameters parser.
    """
    # parse parameters
    parser = ArgumentParser(description="")

    # Main parameters
    # ...

    # Dataset params
    parser.add_argument("--math_operator", type=str, default="+")
    parser.add_argument("--operand_length", type=int, help="for list operations, the length of the lists")
    parser.add_argument("--train_data_pct", type=float, default=5)
    parser.add_argument(
        "--batchsize",
        type=float,
        # default=0.25,
        default=0,
        help="-1 -> entire dataset, 0 -> auto-calculate, 0<N<1 -> fraction of dataset, N>1 -> N",
    )
    parser.add_argument(
        "--datadir",
        type=str,
        default=DEFAULT_DATA_DIR,
    )

    # Model params    
    parser.add_argument("--n_layers", type=int, default=2)
    parser.add_argument("--n_heads", type=int, default=4)
    parser.add_argument("--d_model", type=int, default=128)
    parser.add_argument("--dropout", type=float, default=0.0)
    parser.add_argument("--weight_noise", type=float, default=0.0)
    parser.add_argument("--non_linearity", type=str, default="relu")
    parser.add_argument("--max_context_len", type=int, default=50)

    # Training params
    parser.add_argument("--max_epochs", type=int, default=None)
    parser.add_argument("--max_steps", type=int, default=100000)
    parser.add_argument("--validation_metric", type=str, default="val_accuracy", help="Validation metrics : val_accuracy, val_loss ...")
    parser.add_argument("--save_activations", type=bool_flag, default=True)
    parser.add_argument("--save_outputs", type=bool_flag, default=False)
    parser.add_argument("--logdir", type=str, default="logs")
    parser.add_argument("--save_checkpoint", type=bool_flag, default=True)     
    parser.add_argument("--load_from_ckpt", type=str, default=None)
    parser.add_argument("--save_weights_only", type=bool_flag, default=True)
    parser.add_argument("--eval_only", type=bool_flag, default=False) 
    parser.add_argument("--every_n_epochs", type=int, default=1) 

    # Optimizer
    #parser.add_argument("--opt", type=str, default="adamw", choices=("sgd", "adamw"))
    parser.add_argument("--opt", type=str, default="adamw")
    parser.add_argument("--momentum", type=float, default=0.9)
    parser.add_argument("--warmup_steps", type=int, default=10)
    parser.add_argument("--anneal_lr_steps", type=int, default=100000)
    parser.add_argument("--anneal_lr", type=bool_flag, default=False)
    parser.add_argument("--max_lr", type=float, default=1e-3)
    # LR Scheduler
    parser.add_argument("--lr_scheduler", type=to_none, default="default", help="""
                eg : 
                    - reduce_lr_on_plateau,factor=0.2,patience=20,min_lr=0.00005,mode=min,monitor=val_loss
                    - constant_lr,factor=0.33,total_iters=5,last_epoch=-1
                Using a scheduler is optional but can be helpful.
                The scheduler reduces the LR if the validation performance hasn't improved for the last N (patience) epochs.
                class : reduce_lr_on_plateau, constant_lr, linear_lr, cosine_annealing_lr, exponential_lr, lambda_lr, 
                multiplicative_lr, step_lr, multi_step_lr, cyclic_lr, one_cycle_lr, cosine_annealing_warm_restarts            
    """)
    parser.add_argument("--weight_decay", type=float, default=1)
    parser.add_argument("--weight_decay_kind", type=str, default="to_zero")
    parser.add_argument("--noise_factor", type=float, default=0)
    parser.add_argument("--clip_grad", type=str2dic_all, default="", help="""
    - "gradient_clip_val=float(0.5),gradient_clip_algorithm=str(norm)"
    - "gradient_clip_val=float(0.5),gradient_clip_algorithm=str(value)"
    """)

    # Early_stopping (stop training after grokking)
    parser.add_argument("--early_stopping_grokking", type=str2dic_all, default="", help="""
        * eg. : "patience=int(1000),metric=str(val_accuracy),metric_threshold=float(90.0)"
        * Stop the training `patience` epochs after the `metric` has reached the value `metric_threshold`"
        """)

    # Devices & Seed
    parser.add_argument("--accelerator", type=str, default="auto", help="accelerator types : cpu, gpu, tpu, ipu, auto") 
    parser.add_argument("--devices", type=intorstr, default="auto", help="number of cpu processes, of gpu/tpu cores ...")
    parser.add_argument("--random_seed", type=int, default=-1)

    # wandb
    parser.add_argument("--use_wandb", type=bool_flag, default=False)
    parser.add_argument("--group_name", type=str, default="base")
    parser.add_argument("--wandb_entity", type=str, default=None, help="name of the team on wandb and is optional")
    parser.add_argument("--wandb_project", type=str, default=None, help="name of the project")
    parser.add_argument("--watch", type=str2dic_all, default="", help="""
        https://docs.wandb.ai/ref/python/watch
        eg. : log=str(all),log_freq=int(1),
    """)

    return parser

def get_default_params() :
    train_data_pct=5
    math_operator="+"
    weight_decay=1
    dropout=0.0
    opt="adamw"
    max_lr=0.001
    random_seed=0

    max_steps=100000
    max_epochs=100000
    every_n_epochs=1
    save_weights_only=True

    #lr_scheduler=None
    lr_scheduler="default"
    #lr_scheduler="reduce_lr_on_plateau,factor=0.2,patience=20,min_lr=0.00005,mode=min,monitor=val_loss"

    clip_grad=None
    #clip_grad="gradient_clip_val=float(0.5),gradient_clip_algorithm=str(norm)"
    #clip_grad="gradient_clip_val=float(0.5),gradient_clip_algorithm=str(value)"

    ### wandb ###
    # wandb_entity is the name of the team on wandb and is optional
    # wandb_project is the name of the project
    use_wandb=False
    group_name=f"tdp={train_data_pct}-wd={weight_decay}-d={dropout}-opt={opt}-mlr={max_lr}-mo{math_operator}"
    wandb_entity="grokking_ppsp"
    wandb_project=f"grokking_operator={math_operator}"

    watch=None
    #watch="log=str(all),log_freq=int(1)"

    ### Experiment dump path ###
    dump_path=".."
    logdir=f"{dump_path}/logs/{group_name}"
    datadir=f"{dump_path}/data/{group_name}"

    ### Early_stopping (for grokking) : Stop the training `patience` epochs after the `metric` has reached the value `metric_threshold` ###
    #early_stopping_grokking=$none
    early_stopping_grokking="patience=int(1000),metric=str(val_accuracy),metric_threshold=float(90.0)"

    momentum=0.9
    opttmptmp=f"{opt}"
    params = AttrDict({
        "batchsize" : -1,
        "n_layers" : 2,
        "n_heads" : 4,
        "d_model" : 128,
        "dropout" : dropout,
        "weight_noise" : 0.0,
        "non_linearity" : "relu",
        "max_context_len" : 50,
        "math_operator" : math_operator,
        "train_data_pct" : train_data_pct,
        "warmup_steps" : 10,
        "anneal_lr_steps" : 100000,
        "anneal_lr" : False,
        "max_lr" : max_lr,
        "lr_scheduler" : lr_scheduler,
        "weight_decay" : weight_decay,
        "weight_decay_kind" : "to_zero",
        "noise_factor" : 0,
        "clip_grad" : clip_grad,
        "save_activations" : False,
        "save_outputs" : False,
        "logdir" : logdir,
        "datadir" : datadir,
        "save_checkpoint" : True,
        "use_wandb" : use_wandb,
        "group_name" : group_name,
        "wandb_entity" : wandb_entity,
        "wandb_project" : wandb_project,
        "watch" : watch,
        "opt" : opttmptmp,
        "momentum" : momentum,
        "random_seed" : random_seed,
        "max_steps" : max_steps,
        "validation_metric" : "val_accuracy",
        "max_epochs" : max_epochs,
        "accelerator" : "auto",
        "devices" : "auto",
        "early_stopping_grokking" : str2dic_all(early_stopping_grokking),
        "eval_only" : False,
        "every_n_epochs" : every_n_epochs,
        "save_weights_only" : save_weights_only,
        "operand_length" : None,

        "load_from_ckpt" : None,
        #"load_from_ckpt" : f"{logdir}/checkpoints/last.ckpt",
    })
    return params

class StopTrainingCallback(Callback):
    def on_validation_epoch_end(self, trainer, pl_module):
        early_stopping_patience = pl_module.hparams.early_stopping_grokking.patience
        if pl_module.es_step >= early_stopping_patience :
            #exit()
            raise KeyboardInterrupt

class GenerateCallback(pl.Callback):
    """Use to plot the learned input embeddings at different training stages"""
    
    def __init__(self, every_n_epochs=1):
        super().__init__()
        self.every_n_epochs = every_n_epochs

    def on_epoch_end(self, trainer, pl_module):
    #def on_train_epoch_end(self, trainer, pl_module) :
    #def on_validation_epoch_end(self, trainer, pl_module) :
        pass
        #current_epoch = trainer.current_epoch
        #if current_epoch % self.every_n_epochs == 0 :
         #   pass

def create_data_module(hparams) :
    data_flag = False
    device = "cpu"
    data_module = DataModule(
        train_data_pct = hparams.train_data_pct,  
        math_operator = hparams.math_operator,
        operand_length = hparams.operand_length,
        data_dir = hparams.datadir,
        batch_size = hparams.batchsize,
        device = device,
        flag=data_flag
    )

    train_dataset = data_module.train_dataset
    data_module.train_dataloader()
    data_module.val_dataloader()
    data_module_params = AttrDict({
        "vocab_len" : len(data_module.tokenizer),
        "eq_token_index" : data_module.tokenizer.stoi["="],
        "base_length" : data_module.base_length,

        "train_data_size" : len(train_dataset),
        "train_batchsize" : data_module.train_batchsize,
        "batches_per_epoch_train" : data_module.batches_per_epoch_train,

        "val_data_size" : len(data_module.val_dataset),
        "val_batchsize" : data_module.val_batchsize,
        "batches_per_epoch_val" : data_module.batches_per_epoch_val,
        "data_flag" : data_flag
    })

    setattr(hparams, "data_module_params", data_module_params)

    return data_module, data_flag

def train(hparams: Namespace, data_module : DataModule = None) -> None:
    """
    This is the main trainer_method. This sets up and runs experiment with
    the defined hyperparameters

    :param hparams: An argparse.Namespace with all of the relevant hyperparameters
    """

    print()
    for k, v in vars(hparams).items() : print(k, " --> ", v)
    print()

    # Set up the RNGs for repeatability
    if hparams.random_seed != -1:
        pl.seed_everything(hparams.random_seed, workers=True)

    # set up wandb
    init_wandb(hparams)  
    
    if data_module is None :
        data_module, data_flag = create_data_module(hparams)
  
    # Process the args
    if hparams.logdir is None: hparams.logdir = os.environ.get("LOGDIR", ".")
    hparams.logdir = os.path.abspath(hparams.logdir)

    # Make sure d_model, heads, and d_key are compatible
    assert (
        hparams.d_model % hparams.n_heads == 0
    ), "n_heads=%s does not evenly divide d_model=%s" % (
        hparams.n_heads,
        hparams.d_model,
    )
    hparams.d_key = hparams.d_model / hparams.n_heads

    external_call = getattr(hparams, "external_call", False)
    if external_call :
        os.makedirs(hparams.checkpoint_path, exist_ok=True)
    else :
        checkpoint_path = hparams.logdir + "/checkpoints"
        os.makedirs(checkpoint_path, exist_ok=True)
        #hparams.checkpoint_path = checkpoint_path
        setattr(hparams, "checkpoint_path", checkpoint_path)

        #hparams.save_top_k = -1
        setattr(hparams, "save_top_k", -1)

    # Create the model
    model = TrainableTransformer(hparams).float()

    # if hparams.use_wandb and hparams.watch:
    #     wandb.watch(
    #         model,
    #         #criterion=None,
    #         log = hparams.watch.log,
    #         log_freq = hparams.watch.log_freq,
    #         #idx = None,
    #         #log_graph = False
    #     )

    if FALSE_SAVE_DEBUGGING :
        torch.save(data_module, hparams.logdir + "/data.pt")
        torch.save(hparams, hparams.logdir + "/hparams.pt")

    root_dir = hparams.logdir
    trainer_args = {
        "max_steps": hparams.max_steps,
        "min_steps": hparams.max_steps,
        "max_epochs": hparams.max_epochs, 

        "val_check_interval": 1.0,
        #"profiler": False,
        # "checkpoint_callback": checkpointer,
        #"log_every_n_steps": 1,
        #"flush_logs_every_n_steps": 1000,

        "default_root_dir" : root_dir,

        "accelerator" : hparams.accelerator,
        "devices" : hparams.devices,
        #"reload_dataloaders_every_n_epochs" : True,
        #"weights_summary":"full", # "top", None,

        "strategy" : "ddp",
        #"strategy" : "ddp_spawn",
    }

    #trainer_args["logger"] = CSVLogger(hparams.logdir)
    trainer_args["logger"] = [
        CSVLogger(hparams.logdir),
        TensorBoardLogger(hparams.logdir)
    ]

    callbacks = []
    save_weights_only = getattr(hparams, "save_weights_only", True)
    if not data_flag :
        #early_stopping_patience = hparams.early_stopping_patience
        #patience_metric = hparams.patience_metric
        early_stopping_patience = hparams.early_stopping_grokking.patience
        patience_metric = hparams.early_stopping_grokking.metric
        mode = (lambda s : "min" if 'loss' in s else 'max')(patience_metric)
        early_stopping_callback = EarlyStopping(
            monitor=patience_metric, patience=early_stopping_patience, verbose=False, strict=True,
            mode = mode
        )

        validation_metric = hparams.validation_metric
        mode = (lambda s : "min" if 'loss' in s else 'max')(validation_metric)
        model_checkpoint_callback = ModelCheckpoint(
                dirpath=hparams.checkpoint_path,
                save_weights_only=save_weights_only,
                filename="{epoch}-{%s:.4f}"%validation_metric,
                mode = mode,
                monitor=validation_metric,
                save_top_k=hparams.save_top_k,
                save_last=True,
                every_n_epochs=getattr(hparams, 'every_n_epochs', 1)
        )

        callbacks += [early_stopping_callback, model_checkpoint_callback]
    
    callbacks += [
        #GenerateCallback(), 
        pl.callbacks.LearningRateMonitor("epoch"),
        StopTrainingCallback()
    ]

    trainer_args["callbacks"] = callbacks

    trainer_args["logger"] = [
        TensorBoardLogger(save_dir = root_dir, name='lightning_logs'),
        CSVLogger(save_dir = root_dir, name="csv_logs")
    ]

    if hparams.clip_grad :
        trainer_args["gradient_clip_val"] = hparams.clip_grad.get("gradient_clip_val", 0.0)
        trainer_args["gradient_clip_algorithm"] = hparams.clip_grad.get("gradient_clip_algorithm", "norm")
    
    trainer = Trainer(**trainer_args) #, progress_bar_refresh_rate=0

    trainer.logger._log_graph = False        # If True, we plot the computation graph in tensorboard
    trainer.logger._default_hp_metric = None # Optional logging argument that we don't need
 
    if not hparams.eval_only :
        # Training
        print("Training starts...")
        model.train()

        #trainer.fit(model, datamodule=data_module, ckpt_path=hparams.load_from_ckpt)
        if hparams.load_from_ckpt :
            model = TrainableTransformer.load_from_checkpoint(hparams = hparams, checkpoint_path = hparams.load_from_ckpt).float()
            trainer.fit(model, datamodule=data_module)
            #if save_weights_only :
            #    model = TrainableTransformer.load_from_checkpoint(hparams = hparams, checkpoint_path = hparams.load_from_ckpt).float()
            #    trainer.fit(model, datamodule=data_module)
            #else : 
            #    trainer.fit(model, datamodule=data_module, ckpt_path=hparams.load_from_ckpt)
        else :
            trainer.fit(model, datamodule=data_module)

        print("Training completed.")
        if not data_flag :
            print("Testing starts....")
            model.eval()
            #trainer.test(model, datamodule=data_module)
            trainer.validate(model, datamodule=data_module)
            print("Testing completed.")
    else :
        #hparams.eval_split = "validation"
        setattr(hparams, "eval_split", "validation")
        if not data_flag :
            # Evaluation
            print("Evaluation starts....")
            if hparams.eval_split == "train":
                #data_module.test_dataloader = data_module.train_dataloader
                data_module.val_dataloader = data_module.train_dataloader
            elif hparams.eval_split == "validation" :
                #data_module.test_dataloader = data_module.val_dataloader
                pass
            model.eval()
            #trainer.test(model, datamodule=data_module, ckpt_path=hparams.load_from_ckpt)
            #trainer.validate(model, datamodule=data_module, ckpt_path=hparams.load_from_ckpt)
            trainer.validate(model, data_module.val_dataloader(), ckpt_path=hparams.load_from_ckpt)
            print("Evaluation completed.")

    return hparams.logdir

if __name__ == "__main__":
    # generate parser / parse parameters
    params = get_parser().parse_args()

    # run experiment
    train(params)