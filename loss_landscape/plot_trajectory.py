"""
    Plot the optimization path in the space spanned by principle directions.
"""

from .plot_2D import plot_trajectory as plot_traj
from .projection import setup_PCA_directions_from_point, setup_PCA_directions, project_trajectory
from .model_loader import load
from .net_plotter import get_weights

def plot_trajectory(args, model_files, lightning_module_class, model_file = None) :
    model_file  = model_files[-1] if model_file is None else model_file 
    if model_file is not None :
        net = load(lightning_module_class, model_file = model_file)
        w = get_weights(net) # initial parameters
        s = net.state_dict()

    #--------------------------------------------------------------------------
    # load or create projection directions
    #--------------------------------------------------------------------------
    n_components = getattr(args, "n_components", 2)
    if args.dir_file:
        dir_file = args.dir_file
    else:
        if model_file is None :
            dir_file = setup_PCA_directions(args, model_files, lightning_module_class, n_components=n_components)
        else :
            dir_file = setup_PCA_directions_from_point(args, model_files, w, s, lightning_module_class, n_components=n_components)

    #--------------------------------------------------------------------------
    # projection trajectory to given directions
    #--------------------------------------------------------------------------
    proj_file = project_trajectory(dir_file, w, s, model_files, args.dir_type, 'cos', lightning_module_class)

    plot_traj(proj_file, dir_file)

    return proj_file, dir_file