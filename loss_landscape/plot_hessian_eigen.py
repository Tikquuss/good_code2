"""
    Calculate the hessian matrix of the projected surface and their eigen values.
"""

import copy
import numpy as np
import h5py
import torch
import time
import socket
import torch.nn as nn
import pytorch_lightning as pl
from pytorch_lightning.loggers import CSVLogger
from IPython.display import clear_output

from .plot_surface import name_surface_file, setup_surface_file
from .hess_vec_prod import min_max_hessian_eigs
from .model_loader import load
from .net_plotter import name_direction_file, setup_direction, load_directions, get_weights, set_weights, set_states
from .net_plotter import get_weights, set_weights, set_states
from .projection import cal_angle, nplist_to_tensor
from .scheduler import get_job_indices
from .plot_2D import plot_2d_eig_ratio
from .plot_1D import plot_1d_eig_ratio
from .mpi4pytorch import setup_MPI, barrier, reduce_min, reduce_max
from .evaluation import Evaluator

def get_loss(pl_module, batch, data_size):
    """
    Given a batch of data, this function returns the  loss
    """    
    tmp = pl_module._step(
        batch,
        batch_idx = 0,
        data_size = data_size,
        reduction = "mean",
        grads = False,
    ) 
    loss = tmp[0]
    return loss

def crunch_hessian_eigs(surf_file, net, w, s, d, dataloader, data_size, comm, rank, args, evaluator):
    """
        Calculate eigen values of the hessian matrix of a given model in parallel
        using mpi reduce. This is the synchronized version.
    """
    f = h5py.File(surf_file, 'r+' if rank == 0 else 'r')
    min_eig, max_eig = [], []
    xcoordinates = f['xcoordinates'][:]
    ycoordinates = f['ycoordinates'][:] if 'ycoordinates' in f.keys() else None

    if 'min_eig' not in f.keys():
        shape = xcoordinates.shape if ycoordinates is None else (len(xcoordinates),len(ycoordinates))
        max_eig = -np.ones(shape=shape)
        min_eig = np.ones(shape=shape)
        if rank == 0:
            f['min_eig'] = min_eig
            f['max_eig'] = max_eig
    else:
        min_eig = f['min_eig'][:]
        max_eig = f['max_eig'][:]

    # Generate a list of all indices that need to be filled in.
    # The coordinates of each unfilled index are stored in 'coords'.
    inds, coords, inds_nums = get_job_indices(max_eig, xcoordinates, ycoordinates, comm)
    print('Computing %d values for rank %d'% (len(inds), rank))

    # Loop over all un-calculated coords
    start_time = time.time()
    total_sync = 0.0

    for count, ind in enumerate(inds):
         # Get the coordinates of the points being calculated
        coord = coords[count]

        # Load the weights corresponding to those coordinates into the net
        if args.dir_type == 'weights':
            set_weights(net.module if args.ngpu > 1 else net, w, d, coord)
        elif args.dir_type == 'states':
            set_states(net.module if args.ngpu > 1 else net, s, d, coord)

        # Compute the eign values of the hessian matrix
        compute_start = time.time()
        maxeig, mineig, iter_count = min_max_hessian_eigs(net, dataloader, evaluator, data_size, rank=rank, 
                                                          use_cuda=args.cuda, verbose=True)
        compute_time = time.time() - compute_start

        # Record the result in the local array
        max_eig.ravel()[ind] = maxeig
        min_eig.ravel()[ind] = mineig


        # Send updated plot data to the master node
        sync_start_time = time.time()
        max_eig = reduce_max(comm, max_eig)
        min_eig = reduce_min(comm, min_eig)
        sync_time = time.time() - sync_start_time
        total_sync += sync_time

        # Only the master node writes to the file - this avoids write conflicts
        if rank == 0:
            f['max_eig'][:] = max_eig
            f['min_eig'][:] = min_eig

        print("rank: %d %d/%d  (%0.2f%%)  %d\t  %s \tmaxeig:%8.5f \tmineig:%8.5f \titer: %d \ttime:%.2f \tsync:%.2f" % ( \
            rank, count + 1, len(inds), 100.0 * (count + 1)/len(inds), ind, str(coord), \
            maxeig, mineig, iter_count, compute_time, sync_time))

        if count%10 == 0 : 
            #os.system('cls')
            clear_output(wait=True)

    # This is only needed to make MPI run smoothly. If this process has less work
    # than the rank0 process, then we need to keep calling allreduce so the rank0 process doesn't block
    for i in range(max(inds_nums) - len(inds)):
        max_eig = reduce_max(comm, max_eig)
        min_eig = reduce_min(comm, min_eig)

    total_time = time.time() - start_time
    print('Rank %d done! Total time: %f Sync: %f '%(rank, total_time, total_sync))
    f.close()


def crunch_hessian_eigs_2(surf_file, net, w, s, d, dataloader, data_size, comm, rank, args, evaluator):
    """
        Calculate eigen values of the hessian matrix of a given model in parallel
        using mpi reduce. This is the synchronized version.
    """
    f = h5py.File(surf_file, 'r+' if rank == 0 else 'r')
    min_eig, max_eig = [], []
    xcoordinates = f['xcoordinates'][:]
    ycoordinates = f['ycoordinates'][:] if 'ycoordinates' in f.keys() else None

    if 'min_eig' not in f.keys():
        shape = xcoordinates.shape if ycoordinates is None else (len(xcoordinates),len(ycoordinates))
        max_eig = -np.ones(shape=shape)
        min_eig = np.ones(shape=shape)
        if rank == 0:
            f['min_eig'] = min_eig
            f['max_eig'] = max_eig
    else:
        min_eig = f['min_eig'][:]
        max_eig = f['max_eig'][:]

    if ycoordinates is None : coords = xcoordinates
    else : coords = np.array(np.meshgrid(xcoordinates, ycoordinates)).T.reshape(-1,2) # (N*N,2)
    inds = np.array(range(max_eig.size))

    # Loop over all un-calculated coords
    start_time = time.time()
    total_sync = 0.0

    for count, ind in enumerate(inds):
         # Get the coordinates of the points being calculated
        coord = coords[count]

        # Load the weights corresponding to those coordinates into the net
        if args.dir_type == 'weights':
            set_weights(net.module if args.ngpu > 1 else net, w, d, coord)
        elif args.dir_type == 'states':
            set_states(net.module if args.ngpu > 1 else net, s, d, coord)

        # Compute the eign values of the hessian matrix
        compute_start = time.time()
        maxeig, mineig, iter_count = min_max_hessian_eigs(net, dataloader, evaluator, data_size, rank=rank, 
                                                          use_cuda=args.cuda, verbose=True)
        compute_time = time.time() - compute_start

        # Record the result in the local array
        max_eig.ravel()[ind] = maxeig
        min_eig.ravel()[ind] = mineig


        # Send updated plot data to the master node
        sync_start_time = time.time()
        ###
        ###
        sync_time = time.time() - sync_start_time
        total_sync += sync_time

        print("%d/%d  (%0.2f%%)  %d\t  %s \tmaxeig:%8.5f \tmineig:%8.5f \titer: %d \ttime:%.2f \tsync:%.2f" % ( \
            count + 1, len(inds), 100.0 * (count + 1)/len(inds), ind, str(coord), \
            maxeig, mineig, iter_count, compute_time, sync_time))

        if count%10 == 0 : 
            #os.system('cls')
            clear_output(wait=True)

    f['max_eig'][:] = max_eig
    f['min_eig'][:] = min_eig

    total_time = time.time() - start_time
    print('Rank %d done! Total time: %f Sync: %f '%(rank, total_time, total_sync))
    f.close()


def plot_hessian_eigen(args, lightning_module_class, dataloader, data_size, get_loss) :
    
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


    #--------------------------------------------------------------------------
    # Load models and extract parameters
    #--------------------------------------------------------------------------

    net = load(lightning_module_class, model_file = args.model_file)

    w = get_weights(net) # initial parameters
    s = copy.deepcopy(net.state_dict()) # deepcopy since state_dict are references
    if args.ngpu > 1:
        # data parallel with multiple GPUs on a single node
        net = nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))


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

    #--------------------------------------------------------------------------
    # Start the computation
    #--------------------------------------------------------------------------
    evaluator = Evaluator(get_loss = get_loss)

    if args.mpi: crunch_function = crunch_hessian_eigs
    else : crunch_function = crunch_hessian_eigs_2 
    
    crunch_function(surf_file, net, w, s, d, dataloader, data_size, comm, rank, args, evaluator)
    print ("Rank " + str(rank) + ' is done!')


    #--------------------------------------------------------------------------
    # Plot figures
    #--------------------------------------------------------------------------
    if args.plot and rank == 0:
        if args.y:
            plot_2d_eig_ratio(surf_file, 'min_eig', 'max_eig', args.show)
        else:
            plot_1d_eig_ratio(surf_file, args.xmin, args.xmax, 'min_eig', 'max_eig')

    return dir_file, surf_file

def plot_hessian_eigen_models(args, model_files, lightning_module_class, dataloader, data_size, get_loss, LOG_PATH = None) :
    
    # Setting the seed
    pl.seed_everything(42)

    rank = 0

    # in case of multiple GPUs per node, set the GPU to use for each rank
    if args.cuda:
        if not torch.cuda.is_available():
            raise Exception('User selected cuda option, but cuda is not available on this machine')
        gpu_count = torch.cuda.device_count()
        torch.cuda.set_device(rank % gpu_count)
        print('Rank %d use GPU %d of %d GPUs on %s' % (rank, torch.cuda.current_device(), gpu_count, socket.gethostname()))

    evaluator = Evaluator(get_loss = get_loss)

    """Calculate eigen values of the hessian matrix of a given model."""
    min_eig, max_eig = [], []
    L = len(model_files)

    # Loop over all un-calculated coords
    start_time = time.time()

    tolerance=getattr(args, "tol", 1e-2)
    clear_freq = getattr(args, "clear_freq", 10)
    logger = CSVLogger(LOG_PATH, name="hessian_eigs")
    try:
        for count, f in enumerate(model_files):
            try:
                net = load(lightning_module_class, model_file = f)
                # data parallel with multiple GPUs on a single node
                if args.ngpu > 1: net = nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
                
                # Compute the eign values of the hessian matrix
                compute_start = time.time()
                maxeig, mineig, iter_count = min_max_hessian_eigs(
                    net, dataloader, evaluator, data_size, use_cuda=args.cuda, verbose=True, tol=tolerance
                )
                compute_time = time.time() - compute_start

                # Record the result in the local array
                max_eig.append(maxeig)
                min_eig.append(mineig)
                
                if LOG_PATH :
                    try : epoch = int(f.split("-val_accuracy")[0].split("epoch=")[1])
                    except : epoch = 0
                    logger.log_metrics(metrics={"maxeig" : maxeig, "mineig" : mineig, "epoch" : epoch}, step=count+1)
                    logger.save()

                print("%d/%d  (%0.2f%%) \tmaxeig:%8.5f \tmineig:%8.5f \titer: %d \ttime:%.2f" % 
                    (count + 1, L, 100.0 * (count + 1)/L, maxeig, mineig, iter_count, compute_time))

                if count%clear_freq == 0 : 
                    #os.system('cls')
                    clear_output(wait=True)
                    
            except KeyboardInterrupt:
                pass

    except KeyboardInterrupt:
        pass
    
    total_time = time.time() - start_time
    print('Total time: %f '%total_time)

    return min_eig, max_eig