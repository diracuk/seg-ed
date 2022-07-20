import sys

#sys.path.append('../')
import numpy as np
import pandas as pd
import math
import os
import glob
import matplotlib.pyplot as plt
import scipy.io as sio
import cv2
import json
import openslide

from argparse import (
    ArgumentDefaultsHelpFormatter,
    ArgumentParser,
    Namespace,
    #BooleanOptionalAction,
)

import torch
from torch.nn import Module
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from pystain import StainTransformer

#sys.path.append('../seg-ed')
import unet
from image_dataset import ImageDataset


def parse_command_line_args() -> Namespace:
    """Parse the command-line arguments.

    Returns
    -------
    Namespace
        The command-line arguments.

    """
    parser = ArgumentParser(
        description="Program to generate and merge epithelial masks at 10x magnification \
            with nucleus segmentation data at 40x magnification",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--image_path",
        help="Path to directory containing biopsy image tile. Can include file root / wildcard \
            to return subset of images, but in this case must be enclosed in ''. \
            Default is '/home/ret58/rds/hpc-work/epithelium_slides/images/'",
        type=str,
        default="/home/ret58/rds/hpc-work/epithelium_slides/images/"
    )

    parser.add_argument(
        "--model_path",
        help="Model path + name for epithelium mask model to be used. Default is 'model'",
        type=str,
        default="model"
    )

    parser.add_argument(
        "--json_path",
        help="Path to directory containing json files with nucleus predictions. \
            List of individual files to use will be derived from image file names. \
            Default is '/home/ret58/rds/hpc-work/hover_net/consep_hi_res/json/'",
        type=str,
        default="/home/ret58/rds/hpc-work/hover_net/consep_hi_res/json/"
    )

    parser.add_argument(
        "--bs",
        help="Batch size for running predictions.",
        type=int,
        default=8
    )

    parser.add_argument(
        "--lw",
        help="Loader workers for running predictions.",
        type=int,
        default=4
    )

    return parser.parse_args()


DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device set to: {DEVICE}.")

def get_image_file_names(image_file_path):
    
    if image_file_path[-4:] == ".png":
        image_file_names = glob.glob(image_file_path)
    elif "." not in image_file_path:
        glob_pattern = os.path.join(image_file_path, '*')
        image_file_names = glob.glob(glob_pattern)      
    else:
        raise ValueError("image_path should be a directory or a set of .png files.")

    image_file_names.sort()
    
    return image_file_names


def get_basename(image_file_name):

    return os.path.basename(image_file_name).split('.')[0]


def get_json_name(basename, json_file_path):

    return os.path.join(json_file_path, basename + '.json')



def get_dataset(image_file_names):
    '''
    Get data set from list of file names.

    Parameters
    ----------
    image_file_names : List
        List of file names containing images
    
    Returns
    -------
    data_set : Dataset
        Dataset of all the images, normalized
    '''

    image_transforms = transforms.Compose(
        [StainTransformer(normalise=True)]
        )
    
    data_set = ImageDataset(
        inputs = image_file_names,
        image_transforms = image_transforms
    )

    return data_set

def get_data_loader(data_set, batch_size, loader_workers):
    """Return a dataloader to use in training/validation.

    Parameters
    ----------
    args : Namespace
        Command-line arguments.
    img_set : Image Set to use. 
        "train" or "val"
    data_set : Dataset
        The dataset to be loaded. 

    Returns
    -------
    data_loader : DataLoader
        The requested dataloader.
    """
    data_loader = DataLoader(
        data_set, 
        batch_size=batch_size, 
        shuffle=False,
        num_workers=loader_workers,
    )

    return data_loader


def generate_predictions(model: Module, imgs):

    imgs_gpu = imgs.to(DEVICE)

    outputs = model(imgs_gpu).softmax(dim=1)
    imgs_pred = outputs.to("cpu")

    imgs_pred = 255 * torch.argmax(imgs_pred, dim=1)

    return imgs_pred


def write_preds_to_file(predictions, batch_num, batch_size,
        image_file_names):
    
    if os.path.isdir("tmp") == False:
        os.mkdir("tmp")

    start_file = batch_num * batch_size

    for i in range(predictions.size(dim=0)):
       
        basename = get_basename(image_file_names[start_file + i])
        #basename = os.path.basename(image_file_names[start_file + i]).split('.')[0]
        pred_file_name = "tmp/" + basename + ".npy"

        epi_mask = predictions[i].numpy()
        np.save(pred_file_name, epi_mask)


# Get epithelial preds in batches and write to temp files (one file each?)
def output_predictions(model: Module, data_loader, image_file_names):
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device set to: {DEVICE}.")

    with torch.no_grad():

        for batch_num, imgs in enumerate(data_loader):
            
            imgs_pred = generate_predictions(model, imgs)
            write_preds_to_file(imgs_pred, batch_num, data_loader.batch_size, 
                image_file_names)


# Open predictions (saved as numpy) and scale up
def open_and_rescale_prediction(saved_prediction):
    epi_mask = np.load(saved_prediction)
    epi_mask = cv2.resize(epi_mask, (1136,1136), interpolation = cv2.INTER_NEAREST)

    return epi_mask


def get_epithelium_nuclei(json_file_name, epi_mask):

    with open(json_file_name) as json_file:
        data = json.load(json_file)
    
    nuc_info = data['nuc']

    epi_nuc_uids = []
    epi_nuc_centroids = []
    epi_nuc_contours = []

    for inst in nuc_info:

        inst_info = nuc_info[inst]
        centroid = np.around(inst_info['centroid']).astype(int)
        contour = inst_info['contour']

        if epi_mask[centroid[1], centroid[0]] == 255 and len(contour) >=5:
            epi_nuc_uids.append(inst)
            epi_nuc_centroids.append(centroid)
            epi_nuc_contours.append(contour)
            ## Put type_list here if using

    return epi_nuc_uids, epi_nuc_centroids, epi_nuc_contours


def output_nuclei_stats(tile_id, epi_nuc_uids, epi_nuc_centroids, epi_nuc_contours):
    
    ellipses = [cv2.fitEllipse(np.array(contour)) for contour in epi_nuc_contours]
    contour_areas = [cv2.contourArea(np.array(contour)) for contour in epi_nuc_contours]

    min_diams = [ellipse[1][0] for ellipse in ellipses]
    max_diams = [ellipse[1][1] for ellipse in ellipses]
    diam_ratios = [ellipse[1][0]/ellipse[1][1] for ellipse in ellipses] 
    ellipse_areas = [math.pi * 0.5 * max_diam * 0.5 * min_diam 
                        for max_diam, min_diam in zip(max_diams, min_diams)]

    df = pd.DataFrame(list(zip([tile_id] * len(epi_nuc_uids), epi_nuc_uids, 
                            epi_nuc_centroids, 
                            max_diams, min_diams, 
                            diam_ratios, ellipse_areas, contour_areas)), 
                        columns =['Tile ID', 'Nucl ID', 'Centroid', 'Min diam', 'Max diam', 
                                'Diam ratio', 'Ellipse area', 'Contour area'])
    
    return df


def run_model_for_predictions(model_path, image_file_names, batch_size, loader_workers):

    model = torch.load(model_path, map_location=DEVICE)
    model.eval()

    dataset = get_dataset(image_file_names)
    data_loader = get_data_loader(dataset, batch_size, loader_workers)
    
    output_predictions(model, data_loader, image_file_names)
    # No return statement as outputs predictions to files    


def save_sample_image(image_file, json_file_path, tile_id, epi_mask, epi_nuc_contours):

    hov_dir = os.path.abspath(os.path.join(json_file_path, os.pardir))
    overlay_file = os.path.join(hov_dir, 'overlay', tile_id + '.png')
    overlay = cv2.imread(overlay_file)
    overlay = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)

    image = cv2.imread(image_file)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (1136,1136), interpolation = cv2.INTER_NEAREST)

    ellipses = [cv2.fitEllipse(np.array(contour)) for contour in epi_nuc_contours]
    for ellipse in ellipses:
        image = cv2.ellipse(image, ellipse, (255,0,0), 2)
            
    fig, axes = plt.subplots(ncols = 3, sharey = True, figsize = (18,6))
            
    axes[0].imshow(overlay)  
    axes[1].imshow(epi_mask)
    axes[2].imshow(image)

    for ax in axes:
        ax.axis('off')

    plt.savefig(get_basename(image_file) + "epi_nuc.png")


def loop_through_tiles(image_file_names, json_file_path):

    epi_nuc_data = pd.DataFrame()

    rand_image_file = np.random.choice(image_file_names)

    for image_file in image_file_names:

        #epi_mask = open_and_rescale_prediction('tmp/1182_16_15336_6816.npy')
        #json_file_name = '/home/ret58/rds/hpc-work/hover_net/consep_hi_res/json/1182_16_15336_6816.json'
        #tile_id = '1182_16_15336_6816'
        # Get tile ID
        # Get temp file name
        # Get json file name
        tile_id = get_basename(image_file)
        json_file_name = get_json_name(tile_id, json_file_path)
        temp_file = os.path.join('tmp', tile_id + '.npy')

        if not os.path.exists(json_file_name):
            print(f"File {json_file_name} does not exist.")
            continue
                 
        epi_mask = open_and_rescale_prediction(temp_file)
        epi_nuc_uids, epi_nuc_centroids, epi_nuc_contours = get_epithelium_nuclei(json_file_name, epi_mask)
            
        if len(epi_nuc_uids) == 0:
            print(f"No epithelial nuclei identified in file {json_file_name}.")
            continue

        df = output_nuclei_stats(tile_id, epi_nuc_uids, epi_nuc_centroids, epi_nuc_contours)
        df.to_pickle('tmp/epi_nuc_' + tile_id + '.pkl')

        epi_nuc_data = pd.concat([epi_nuc_data, df], ignore_index = True)

        if image_file == rand_image_file:
            save_sample_image(image_file, json_file_path, tile_id, epi_mask, epi_nuc_contours)
      
    epi_nuc_data.to_pickle('epi_nuc_data.pkl')

    return epi_nuc_data


if __name__ == "__main__":

    command_line_args = parse_command_line_args()

    image_file_names = get_image_file_names(command_line_args.image_path)
    print(image_file_names[0])

    run_model_for_predictions(command_line_args.model_path, image_file_names, 
        command_line_args.bs, command_line_args.lw)

    epi_nuc_data = loop_through_tiles(image_file_names, command_line_args.json_path)

    print("Head and tail of final dataframe:")
    print(epi_nuc_data.head())
    print(epi_nuc_data.tail())
