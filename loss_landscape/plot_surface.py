"""
    Calculate and visualize the loss surface.
    Usage example:
    >>  python plot_surface.py --x=-1:1:101 --y=-1:1:101 --model resnet56 --cuda
"""

# PyTorch Lightning
import pytorch_lightning as pl

# import argparse
import copy
import h5py
import torch
import time
import socket
import os
# import sys
import numpy as np
import torch.nn as nn
from IPython.display import clear_output

import pickle

from .model_loader import load
from .net_plotter import name_direction_file, setup_direction, load_directions
from .net_plotter import get_weights, set_weights, set_states
from .projection import cal_angle, nplist_to_tensor
from .scheduler import get_job_indices
from .plot_2D import plot_contour_trajectory, plot_2d_contour
from .plot_1D import plot_1d_loss_err
from .mpi4pytorch import setup_MPI, barrier, reduce_max
from .evaluation import Evaluator

def name_surface_file(args, dir_file):
    # skip if surf_file is specified in args
    if args.surf_file:
        return args.surf_file

    # use args.dir_file as the perfix
    surf_file = dir_file

    # resolution
    surf_file += '_[%s,%s,%d]' % (str(args.xmin), str(args.xmax), int(args.xnum))
    if args.y:
        surf_file += 'x[%s,%s,%d]' % (str(args.ymin), str(args.ymax), int(args.ynum))

    # dataloder parameters
    if args.raw_data: # without data normalization
        surf_file += '_rawdata'
    if args.data_split > 1:
        surf_file += '_datasplit=' + str(args.data_split) + '_splitidx=' + str(args.split_idx)

    return surf_file + ".h5"

def setup_surface_file(args, surf_file, dir_file):
    # skip if the direction file already exists
    if os.path.exists(surf_file):
        f = h5py.File(surf_file, 'r')
        if (args.y and 'ycoordinates' in f.keys()) or 'xcoordinates' in f.keys():
            f.close()
            print ("%s is already set up" % surf_file)
            return

    f = h5py.File(surf_file, 'a')
    f['dir_file'] = dir_file

    # Create the coordinates(resolutions) at which the function is evaluated
    xcoordinates = np.linspace(args.xmin, args.xmax, num=args.xnum)
    f['xcoordinates'] = xcoordinates

    if args.y:
        ycoordinates = np.linspace(args.ymin, args.ymax, num=args.ynum)
        f['ycoordinates'] = ycoordinates
    f.close()

    return surf_file


def crunch(surf_file, net, w, s, d, dataloaders, loss_keys, acc_keys, comm, rank, args, evaluator):
    """
        Calculate the loss values and/or accuracies of modified models in parallel
        using MPI reduce.
    """

    assert len(dataloaders) == len(loss_keys) == len(acc_keys)

    f = h5py.File(surf_file, 'r+' if rank == 0 else 'r')
    losses, accuracies = {}, {}
    xcoordinates = f['xcoordinates'][:]
    ycoordinates = f['ycoordinates'][:] if 'ycoordinates' in f.keys() else None

    shape = xcoordinates.shape if ycoordinates is None else (len(xcoordinates),len(ycoordinates))
    for loss_key, acc_key in zip(loss_keys, acc_keys) :
        if loss_key not in f.keys():
            losses[loss_key] = -np.ones(shape=shape)
            accuracies[acc_key] = -np.ones(shape=shape)
            f[loss_key] = losses[loss_key]
            f[acc_key] = accuracies[acc_key]
        else:
            losses[loss_key] = f[loss_key][:]
            accuracies[acc_key] = f[acc_key][:]
        
    # Generate a list of indices of 'losses' that need to be filled in.
    # The coordinates of each unfilled index (with respect to the direction vectors
    # stored in 'd') are stored in 'coords'.
    inds, coords, inds_nums = get_job_indices(losses[loss_keys[0]], xcoordinates, ycoordinates, comm)

    print('Computing %d values for rank %d'% (len(inds), rank))
    start_time = time.time()
    total_sync = 0.0

    # Loop over all uncalculated loss values
    for count, ind in enumerate(inds):
        # Get the coordinates of the loss value being calculated
        coord = coords[count]

        # Load the weights corresponding to those coordinates into the net
        if args.dir_type == 'weights': 
            set_weights(net.module if args.ngpu > 1 else net, w, d, coord)
        elif args.dir_type == 'states':
            set_states(net.module if args.ngpu > 1 else net, s, d, coord)

        loss_compute_time = 0
        syc_time = 0
        for dataloader, loss_key, acc_key in zip(dataloaders, loss_keys, acc_keys) :

            # Record the time to compute the loss value
            loss_start = time.time()
            loss, acc = evaluator(net, dataloader)
            loss_compute_time += time.time() - loss_start

            # Record the result in the local array
            losses[loss_key].ravel()[ind] = loss
            accuracies[acc_key].ravel()[ind] = acc

            # Send updated plot data to the master node
            syc_start = time.time()

            losses[loss_key]     = reduce_max(comm, losses[loss_key])
            accuracies[acc_key] = reduce_max(comm, accuracies[acc_key])

            syc_time += time.time() - syc_start
        
        total_sync += syc_time

        # Only the master node writes to the file - this avoids write conflicts
        if rank == 0:
            for loss_key, acc_key in zip(loss_keys, acc_keys) :
                f[loss_key][:] = losses[loss_key]
                f[acc_key][:] = accuracies[acc_key]
            f.flush()

        print('Evaluating rank %d  %d/%d  (%.1f%%)  coord=%s \t%s= %.3f \t%s=%.2f \ttime=%.2f \tsync=%.2f' % (
                rank, count+1, len(inds), 100.0 * count/len(inds), str(coord), loss_key, loss,
                acc_key, acc, loss_compute_time, syc_time))
        
        if count%10 == 0 : 
            #os.system('cls')
            clear_output(wait=True)

    # This is only needed to make MPI run smoothly. If this process has less work than
    # the rank0 process, then we need to keep calling reduce so the rank0 process doesn't block
    for i in range(max(inds_nums) - len(inds)):
        losses[loss_key] = reduce_max(comm, losses[loss_key])
        accuracies[acc_key] = reduce_max(comm, accuracies[acc_key])

    total_time = time.time() - start_time
    print('Rank %d done!  Total time: %.2f Sync: %.2f' % (rank, total_time, total_sync))

    f.close()


def crunch_2(surf_file, net, w, s, d, dataloaders, loss_keys, acc_keys, comm, rank, args, evaluator):
    """
        Calculate the loss values and/or accuracies of modified models
    """

    assert len(dataloaders) == len(loss_keys) == len(acc_keys)

    f = h5py.File(surf_file, 'r+' if rank == 0 else 'r')
    losses, accuracies = {}, {}
    xcoordinates = f['xcoordinates'][:]
    ycoordinates = f['ycoordinates'][:] if 'ycoordinates' in f.keys() else None

    shape = xcoordinates.shape if ycoordinates is None else (len(xcoordinates),len(ycoordinates))
    for loss_key, acc_key in zip(loss_keys, acc_keys) :
        if loss_key not in f.keys():
            losses[loss_key] = -np.ones(shape=shape)
            accuracies[acc_key] = -np.ones(shape=shape)
            f[loss_key] = losses[loss_key]
            f[acc_key] = accuracies[acc_key]
        else:
            losses[loss_key] = f[loss_key][:]
            accuracies[acc_key] = f[acc_key][:]
    
    if ycoordinates is None : coords = xcoordinates
    else : coords = np.array(np.meshgrid(xcoordinates, ycoordinates)).T.reshape(-1,2) # (N*N,2)
    #inds = np.array(range(losses.size))
    inds = np.array(range(losses[loss_keys[0]].size))

    start_time = time.time()
    total_sync = 0.0

    # Loop over all uncalculated loss values
    for count, ind in enumerate(inds):
        # Get the coordinates of the loss value being calculated
        coord = coords[count]

        # Load the weights corresponding to those coordinates into the net
        if args.dir_type == 'weights': 
            set_weights(net.module if args.ngpu > 1 else net, w, d, coord)
        elif args.dir_type == 'states':
            set_states(net.module if args.ngpu > 1 else net, s, d, coord)

        loss_compute_time = 0
        for dataloader, loss_key, acc_key in zip(dataloaders, loss_keys, acc_keys) :
            # Record the time to compute the loss value
            loss_start = time.time()
            loss, acc = evaluator(net, dataloader)
            loss_compute_time += time.time() - loss_start

            # Record the result in the local array
            losses[loss_key].ravel()[ind] = loss
            accuracies[acc_key].ravel()[ind] = acc

        # TODO
        print('Evaluating %d/%d  (%.1f%%)  coord=%s \t%s= %.3f \t%s=%.2f \ttime=%.2f' % (
                count+1, len(inds), 100.0 * count/len(inds), str(coord), loss_key, loss,
                acc_key, acc, loss_compute_time))
        
        if count%10 == 0 : 
            #os.system('cls')
            clear_output(wait=True)

    for loss_key, acc_key in zip(loss_keys, acc_keys) :
        f[loss_key][:] = losses[loss_key]
        f[acc_key][:] = accuracies[acc_key]
    f.flush()

    total_time = time.time() - start_time
    print('Done!  Total time: %.2f Sync: %.2f' % (total_time, total_sync))

    f.close()

#########################################################################
# plot surface
#########################################################################

def init_plot_surface(args) :
    # Setting the seed
    pl.seed_everything(42)

    #--------------------------------------------------------------------------
    # Environment setup
    #--------------------------------------------------------------------------
    if args.mpi:
        comm = setup_MPI()
        rank, nproc = comm.Get_rank(), comm.Get_size()
    else:
        comm, rank, nproc = None, 0, 1

    # in case of multiple GPUs per node, set the GPU to use for each rank
    if args.cuda:
        if not torch.cuda.is_available():
            raise Exception('User selected cuda option, but cuda is not available on this machine')
        gpu_count = torch.cuda.device_count()
        torch.cuda.set_device(rank % gpu_count)
        print('Rank %d use GPU %d of %d GPUs on %s' % (rank, torch.cuda.current_device(), gpu_count, socket.gethostname()))

    #--------------------------------------------------------------------------
    # Check plotting resolution
    #--------------------------------------------------------------------------
    try:
        args.xmin, args.xmax, args.xnum = [float(a) for a in args.x.split(':')]
        args.xnum = int(args.xnum)
        args.ymin, args.ymax, args.ynum = (None, None, None)
        if args.y:
            args.ymin, args.ymax, args.ynum = [float(a) for a in args.y.split(':')]
            assert args.ymin and args.ymax and args.ynum, 'You specified some arguments for the y axis, but not all'
            args.ynum = int(args.ynum)
    except:
        raise Exception('Improper format for x- or y-coordinates. Try something like -1:1:51')
    
    return args, comm, rank 
    
def get_net(lightning_module_class, model_file, ngpu):
    #--------------------------------------------------------------------------
    # Load models and extract parameters
    #--------------------------------------------------------------------------
    net = load(lightning_module_class, model_file = model_file)
    w = get_weights(net) # initial parameters
    s = copy.deepcopy(net.state_dict()) # deepcopy since state_dict are references
    if ngpu > 1:
        # data parallel with multiple GPUs on a single node
        net = nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
    return net, w, s

def setup_dir_file(args, rank, net, comm) :
    #--------------------------------------------------------------------------
    # Setup the direction file and the surface file
    #--------------------------------------------------------------------------
    dir_file = name_direction_file(args) # name the direction file
    if rank == 0:
        setup_direction(args, dir_file, net)

    surf_file = name_surface_file(args, dir_file)
    if rank == 0:
        setup_surface_file(args, surf_file, dir_file)

    # wait until master has setup the direction file and surface file
    barrier(comm)

    # load directions
    d = load_directions(dir_file)
    # calculate the consine similarity of the two directions
    if len(d) == 2 and rank == 0:
            similarity = cal_angle(nplist_to_tensor(d[0]), nplist_to_tensor(d[1]))
            print('cosine similarity between x-axis and y-axis: %f' % similarity)

    barrier(comm)

    return args, dir_file, surf_file, d

def plot_surface(args, lightning_module_class, metrics, train_dataloader = None, test_dataloader = None, save_to = None) :

    assert train_dataloader or test_dataloader

    # Setting the seed
    pl.seed_everything(42)
    if save_to : os.makedirs(save_to, exist_ok=True)

    #--------------------------------------------------------------------------
    # Environment setup & Check plotting resolution
    #--------------------------------------------------------------------------
    args, comm, rank = init_plot_surface(args)

    #--------------------------------------------------------------------------
    # Load models and extract parameters
    #--------------------------------------------------------------------------
    net, w, s = get_net(lightning_module_class, args.model_file, args.ngpu)

    #--------------------------------------------------------------------------
    # Setup the direction file and the surface file
    #--------------------------------------------------------------------------
    args, dir_file, surf_file, d = setup_dir_file(args, rank, net, comm)

    #--------------------------------------------------------------------------
    # Start the computation
    #--------------------------------------------------------------------------
    evaluator = Evaluator(metrics = metrics)

    if args.mpi: crunch_function = crunch
    else : crunch_function = crunch_2 

    dataloaders, loss_keys, acc_keys = [], [], []
    if train_dataloader :
        #crunch_function(surf_file, net, w, s, d, train_dataloader , 'train_loss', 'train_acc', comm, rank, args, evaluator)
        dataloaders, loss_keys, acc_keys = [train_dataloader], ['train_loss'], ['train_acc']
    if test_dataloader :
        #crunch_function(surf_file, net, w, s, d, test_dataloader, 'test_loss', 'test_acc', comm, rank, args, evaluator)
        dataloaders.append(test_dataloader)
        loss_keys.append('test_loss')
        acc_keys.append('test_acc')
    crunch_function(surf_file, net, w, s, d, dataloaders, loss_keys, acc_keys, comm, rank, args, evaluator)

    #--------------------------------------------------------------------------
    # Plot figures
    #--------------------------------------------------------------------------
    if args.plot and rank == 0:
        if args.y and args.proj_file:
            print("======= 1 ========== ")
            print(surf_file)
            print(dir_file)
            print(args.proj_file)
            plot_contour_trajectory(surf_file, dir_file, args.proj_file, 'train_loss', args.show, save_to=save_to)
        elif args.y:
            plot_2d_contour(surf_file, 'train_loss', args.vmin, args.vmax, args.vlevel, args.show, save_to=save_to)
        else:
            plot_1d_loss_err(surf_file, args.xmin, args.xmax, args.loss_max, args.acc_max, args.log, args.show, save_to=save_to)
    
    return dir_file, surf_file

def get_data_from_file(surf_file, plot_type):
    f = h5py.File(surf_file,'r')
    #print(f.keys())

    if plot_type == "1d":
        assert 'train_loss' in f.keys(), "'train_loss' does not exist"
        data = {
            'xcoordinates' : f['xcoordinates'][:],
            'train_loss' : f['train_loss'][:],
            'train_acc' : f['train_acc'][:],
        }
        if 'test_loss' in f.keys():
            data['test_loss'] = f['test_loss'][:]
            data['test_acc'] = f['test_acc'][:]

    if plot_type == "2d": pass
    if plot_type == "traj" : pass

    f.close()
    return data


def plot_surface_list(args, lightning_module_class, metrics, train_dataloader = None, test_dataloader = None, save_to = None) :

    assert train_dataloader or test_dataloader
    epochs = args.epochs
    model_files = args.model_files
    direction = args.direction

    pl.seed_everything(42)
    if save_to : os.makedirs(save_to, exist_ok=True)
    #--------------------------------------------------------------------------
    # Environment setup & Check plotting resolution
    #--------------------------------------------------------------------------
    args, comm, rank = init_plot_surface(args)

    all_data = {}
    dir_files, surf_files = {}, {}
    for epoch in epochs :
        
        args.model_file = model_files[epoch]
        if direction == 'random' : args.model_file2 = ""
        elif direction == 'from_init' : args.model_file2 =  model_files[0]
        elif direction == 'i-1' : args.model_file2 = model_files[epoch-1]
        elif direction == 'i+1' : args.model_file2 =  model_files[epoch+1]
        elif direction == 'until_end' : args.model_file2 =  model_files[-1]
        else : raise RuntimeError("Wrong direction!")
        
        #--------------------------------------------------------------------------
        # Load models and extract parameters
        #--------------------------------------------------------------------------
        net, w, s = get_net(lightning_module_class, args.model_file, args.ngpu)

        #--------------------------------------------------------------------------
        # Setup the direction file and the surface file
        #--------------------------------------------------------------------------
        args, dir_file, surf_file, d = setup_dir_file(args, rank, net, comm)

        #--------------------------------------------------------------------------
        # Start the computation
        #--------------------------------------------------------------------------
        evaluator = Evaluator(metrics = metrics)

        if args.mpi: crunch_function = crunch
        else : crunch_function = crunch_2 

        dataloaders, loss_keys, acc_keys = [], [], []
        if train_dataloader :
            #crunch_function(surf_file, net, w, s, d, train_dataloader , 'train_loss', 'train_acc', comm, rank, args, evaluator)
            dataloaders, loss_keys, acc_keys = [train_dataloader], ['train_loss'], ['train_acc']
        if test_dataloader :
            #crunch_function(surf_file, net, w, s, d, test_dataloader, 'test_loss', 'test_acc', comm, rank, args, evaluator)
            dataloaders.append(test_dataloader)
            loss_keys.append('test_loss')
            acc_keys.append('test_acc')
        crunch_function(surf_file, net, w, s, d, dataloaders, loss_keys, acc_keys, comm, rank, args, evaluator)
        

        if args.y and args.proj_file:
            all_data[epoch] = get_data_from_file(surf_file, plot_type="traj")
            plot_contour_trajectory(surf_file, dir_file, args.proj_file, 'train_loss', args.show, save_to=f"{save_to}/{epoch}")
        elif args.y:
            all_data[epoch] = get_data_from_file(surf_file, plot_type="2d")
            plot_2d_contour(surf_file, 'train_loss', args.vmin, args.vmax, args.vlevel, args.show, save_to=f"{save_to}/{epoch}")
        else:
            all_data[epoch] = get_data_from_file(surf_file, plot_type="1d")
            plot_1d_loss_err(surf_file, args.xmin, args.xmax, args.loss_max, args.acc_max, args.log, args.show, save_to=f"{save_to}/{epoch}")

        dir_files[epoch] = dir_file
        surf_files[epoch] = surf_file

    filehandler = open(os.path.join(save_to, "data.pkl"), "wb")   
    pickle.dump({'data':all_data, 'dir_files':dir_files, 'surf_files':surf_files}, filehandler)
    filehandler.close()

    return all_data, dir_files, surf_files

