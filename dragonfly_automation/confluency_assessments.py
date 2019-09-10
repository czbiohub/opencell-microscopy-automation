import os
import json
import skimage
import sklearn
import tifffile
import datetime
import numpy as np

from scipy import ndimage
from sklearn import cluster
from sklearn import metrics
from skimage import feature
from skimage import morphology
from matplotlib import pyplot as plt

from dragonfly_automation import utils


def assess_confluency(snap, log_dir=None, position_ind=None):
    '''
    Assess confluency of a single FOV given a single z-slice
    that is potentially somewhat out-of-focus

    Parameters
    ----------
    snap : numpy array
        an image of the current z-slice (assumed to be 2D and uint16)
    log_dir : str, optional
        Local path to the directory in which to save log files
        (if None, no logging is performed)
    position_ind : int, optional (but required for logging)
        The index of the current position (used only for logging)
        Note that we use an index, and not a label, because it's not clear
        that we can assume that the labels in the position list will always be unique 
        (e.g., they may not be when the position list is generated manually,
        rather than by the HCS Site Generator plugin)
        
    Returns
    -------
    confluency_is_good : bool
        whether the confluency looks 'good'
    confluency_label : str
        details about the confluency
        'good', 'low', 'high', 'anisotropic'
    '''

    # parameters used for decision tree
    # min/max number of nuclei in the FOV
    min_num_nuclei = 20
    max_num_nuclei = 55

    # max offset of the center of mass and and max asymmetry (eigenvalue ratio)
    max_rel_com_offset = .3
    max_eval_ratio = 1.0

    # hard-coded approximate nucleus radius
    nucleus_radius = 15

    # hard-coded image dimensions
    image_size = 1024

    # default values
    confluency_label = 'good'
    confluency_is_good = True

    # find the positions of the nuclei in the image
    mask = _generate_background_mask(snap)
    nucleus_positions = _find_nucleus_positions(mask, nucleus_radius)

    # calculate some properties of the spatial distribution of nucleus positions
    num_nuclei, rel_com_offset, eval_ratio = _calculate_nucleus_position_features(
        nucleus_positions, image_size)

    # very rudimentary logic to assess confluency using these properties
    # too many nuclei
    if num_nuclei > max_num_nuclei:
        confluency_label = 'high'
        confluency_is_good = False

    # too few nuclei
    elif num_nuclei < min_num_nuclei:
        confluency_label = 'low'
        confluency_is_good = False

    # distribution of nuclei is not isotropic
    elif rel_com_offset > max_rel_com_offset or eval_ratio > max_eval_ratio:
        confluency_label = 'anisotropic'
        confluency_is_good = False
    
    if log_dir is not None:

        # computed properties to log
        properties = {
            'num_nuclei': num_nuclei,
            'rel_com_offset': rel_com_offset,
            'eval_ratio': eval_ratio,
            'confluency_label': confluency_label,
        }

        log_dir = os.path.join(log_dir, 'confluency-check')
        _log_confluency_data(
            snap, properties, nucleus_positions, log_dir, position_ind)

    return confluency_is_good, confluency_label



def _log_confluency_data(snap, properties, nucleus_positions, log_dir, position_ind):
    '''
    '''

    sep = ','

    # make the directory for the snaps
    snap_dir = os.path.join(log_dir, 'confluency-snaps')
    os.makedirs(snap_dir, exist_ok=True)

    # filename for the snap itself
    def snap_filename(tag):
        return 'confluency_snap_pos%05d_%s.tif' % (position_ind, tag)

    # create the row to append to the logfile
    row = {'snap_filename': snap_filename('RAW'), 'position_ind': position_ind}
    row.update(properties)

    # create the CSV-like log file if it does not exist
    log_filepath = os.path.join(log_dir, 'confluency-check-log.csv')
    if not os.path.isfile(log_filepath):
        with open(log_filepath, 'w') as file:
            file.write('%s\n' % sep.join(row.keys()))

    # append the new row
    with open(log_filepath, 'a') as file:
        file.write('%s\n' % sep.join(map(str, row.values())))

    # save the raw snap image
    tifffile.imwrite(os.path.join(snap_dir, snap_filename('RAW')), snap.astype('uint16'))
    
    # save an autogained version of the snap (to facilitate previewing the image)
    snap = _to_uint8(snap)
    tifffile.imwrite(os.path.join(snap_dir, snap_filename('UINT8')), snap)

    # crudely annotate the snap image by marking the positions of the nuclei
    width = 3
    maxx = snap.max()
    shape = snap.shape

    # lower the brightness of the autogained snap
    snap = (snap/2).astype('uint8')

    for pos in nucleus_positions:
        snap[
            int(max(0, pos[0] - width)):int(min(shape[0], pos[0] + width)), 
            int(max(0, pos[1] - width)):int(min(shape[1], pos[1] + width))] = maxx

        tifffile.imwrite(os.path.join(snap_dir, snap_filename('ANT')), snap)


def _to_uint8(im):

    dtype = 'uint8'
    max_value = 255

    im = im.copy().astype(float)

    percentile = 1
    minn, maxx = np.percentile(im, (percentile, 100 - percentile))
    if minn==maxx:
        return (im * 0).astype(dtype)

    im = im - minn
    im[im < minn] = 0
    im = im/(maxx - minn)
    im[im > 1] = 1
    im = (im * max_value).astype(dtype)
    return im


def _generate_background_mask(im):
    '''
    '''
    # smooth the raw image
    imf = skimage.filters.gaussian(im, sigma=5)

    # background mask from minimum cross-entropy
    mask = imf > skimage.filters.threshold_li(imf)

    # remove regions that are too small to be nuclei
    min_region_area = 1000
    mask_label = skimage.measure.label(mask)
    props = skimage.measure.regionprops(mask_label)
    for prop in props:
        if prop.area < min_region_area:
            mask[mask_label==prop.label] = False

    return mask


def _find_nucleus_positions(mask, nucleus_radius):
    '''

    '''
    # smoothed distance transform
    dist = ndimage.distance_transform_edt(mask)
    distf = skimage.filters.gaussian(dist, sigma=1)

    # the positions of the local maximima in the distance transform
    # correspond roughly to the centers of mass of the individual nuclei
    local_max_inds = skimage.feature.peak_local_max(
        distf, indices=True, min_distance=nucleus_radius, labels=mask)

    return local_max_inds


def _show_nucleus_positions(positions, im=None, ax=None):
    '''
    Plot the nucleus positions, 
    optionally overlaid on the image itself and the background mask
    '''
    
    if ax is None:
        plt.figure(figsize=(10, 10))
        ax = plt.gca()

    # show the image and the background mask
    if im is not None:
        mask = _generate_background_mask(im)
        ax.imshow(
            skimage.color.label2rgb(~mask, image=_to_uint8(im), colors=('black', 'yellow')))

    # plot the positions themselves
    ax.scatter(positions[:, 1], positions[:, 0], color='red')
    ax.set_xlim([0, 1024])
    ax.set_ylim([0, 1024])
    ax.set_aspect('equal')


def _calculate_nucleus_position_features(positions, image_size):
    '''

    '''

    # the number of nuclei
    num_nuclei = positions.shape[0]
    
    # the distance of the center of mass from the center of the image
    com_offset = ((positions.mean(axis=0) - (image_size/2))**2).sum()**.5
    rel_com_offset = com_offset / image_size

    # eigenvalues of the covariance matrix
    evals, evecs = np.linalg.eig(np.cov(positions.transpose()))

    # the ratio of eigenvalues is a measure of asymmetry 
    eval_ratio = (max(evals) - min(evals))/min(evals)

    return num_nuclei, rel_com_offset, eval_ratio


def _calculate_mask_features(positions, image_size, nucleus_radius):
    '''
    Calculate properties of a simulated nucleus mask
    (obtained by thresholding the distance transform of the nucleus positions)

    Note that it is necessary to simulate a nucleus mask,
    rather than use the mask returned by _generate_background_mask, 
    because the area of the foreground regions in the generated mask
    depends on the focal plane.

    '''

    position_mask = np.zeros((image_size, image_size))
    for position in positions:
        position_mask[position[0], position[1]] = 1

    mask = ndimage.distance_transform_edt(~position_mask.astype(bool)) > nucleus_radius
    props = skimage.measure.regionprops(skimage.measure.label(mask))

    num_regions = len(props)
    total_area = mask.sum() / (mask.shape[0]*mask.shape[1])
    median_region_area = np.median([p.area for p in props])
    max_distance = ndimage.distance_transform_edt(~mask).max()

    return num_regions, total_area, median_region_area, max_distance


def _calculate_nucleus_cluster_features(positions, image_size):
    '''
    Cluster positions using DBSCAN 
    and calculate various measures of cluster homogeneity
    '''
    
    # empirically-selected parameters for dbscan
    eps = 0.1
    min_samples = 3

    dbscan = sklearn.cluster.DBSCAN(eps=eps, min_samples=min_samples, metric='euclidean')
    dbscan.fit(positions/image_size)
    labels = dbscan.labels_

    cluster_labels = set(labels)
    num_clusters = len(cluster_labels)
    num_unclustered = (labels==-1).sum()

    if num_clusters > 1:
        sil_score = sklearn.metrics.silhouette_score(positions, labels)
        db_score = sklearn.metrics.davies_bouldin_score(positions, labels)
        ch_score = sklearn.metrics.calinski_harabasz_score(positions, labels)
    else:
        sil_score, db_score, ch_score = None, None, None

    return num_clusters, num_unclustered, sil_score, db_score, ch_score