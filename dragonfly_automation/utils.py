import os
import re
import sys
import py4j
import json
import skimage
import datetime
import numpy as np

from scipy import interpolate
from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import Axes3D as ax3

from dragonfly_automation import operations, utils


def timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def to_uint8(im):

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


def interpolate_focusdrive_positions_from_corners(
    position_list_filepath, 
    region_shape, 
    num_positions_per_well,
    corner_positions):
    '''
    This method refactors the 'StageTiltDragonfly.py' script

    Parameters
    ----------
    position_list_filepath: str
        Local path to a JSON list of positions; these positions are assumed 
        to have been generated by the HCS Site Generator plugin
    region_shape: tuple of (num_rows, num_columns)
        The size/shape of the plate 'region' to be imaged
    num_positions_per_well : int
        The number of positions per well
    corner_positions: tuple of tuples
        The user-measured z-positions (FocusDrive device positions) 
        of the corners of the plate region, as a tuple of the form
        (
            (top_left, top_right), 
            (bottom_left, bottom_right),
        )

    Returns
    -------
    filepath : the path to the interpolated position list
    position_list : the position list itself

    '''
    num_rows, num_cols = region_shape

    # linearly interpolate the z-positions
    rows = [0, 0, num_rows, num_rows]
    cols = [0, num_cols, 0, num_cols]
    z = np.array(corner_positions).flatten()
    interpolator = interpolate.interp2d(rows, cols, z, kind='linear')

    with open(position_list_filepath) as file:
        position_list = json.load(file)

    # loop over each well in the region
    for row_ind in range(num_rows):
        for col_ind in range(num_cols):

            # Here we account for the snake-like order of the positions
            # in the position_list generated by the HCS Site Generator plugin
            # This list begins with the positions in the top-left-most well,
            # then traverses the top-most row of wells from right to left, 
            # then traverses the second row from left to right, and so on
            if row_ind % 2 == 0:
                physical_col_ind = (num_cols - 1) - col_ind
            else:
                physical_col_ind = col_ind
            
            # the interpolated z-position of the current well
            interpolated_position = interpolator(row_ind, physical_col_ind)[0]

            # the config entry for the 'FocusDrive' device (this is the motorized z-stage)
            focusdrive_config = {
                'X': interpolated_position,
                'Y': 0,
                'Z': 0,
                'AXES': 1,
                'DEVICE': 'FocusDrive',
            }

            # copy the FocusDrive config into the position_list
            # at each position of the current well
            for pos_ind in range(num_positions_per_well):
                ind = num_positions_per_well * (row_ind * num_cols + col_ind) + pos_ind
                position_list['POSITIONS'][ind]['DEVICES'].append(focusdrive_config)
    
    # save the new position_list
    ext = position_list_filepath.split('.')[-1]
    new_filepath = re.sub('.%s$' % ext, '_INTERPOLATED.%s' % ext, position_list_filepath)
    with open(new_filepath, 'w') as file:
        json.dump(position_list, file)

    return new_filepath, position_list



def well_id_to_position(well_id):
    '''
    'A1' to (0, 0), 'H12' to (7, 11), etc
    '''
    pattern = r'^([A-H])([0-9]{1,2})$'
    result = re.findall(pattern, well_id)
    row, col = result[0]
    row_ind = list('ABCDEFGH').index(row)
    col_ind = int(col) - 1
    return row_ind, col_ind


def parse_hcs_site_label(label):
    '''
    Parse an HCS site label
    ** copied from PipelinePlateProgram **
    '''
    pattern = r'^([A-H][0-9]{1,2})-Site_([0-9]+)$'
    result = re.findall(pattern, label)
    well_id, site_num = result[0]
    site_num = int(site_num)
    return well_id, site_num


def find_nearest_well(mmc, position_list):
    '''
    '''
    # current xy stage position
    current_pos = mmc.getXPosition('XYStage'), mmc.getYPosition('XYStage')

    # find the well closest the current position
    dists = []
    for ind, p in enumerate(position_list['POSITIONS']):
        xystage = [d for d in p['DEVICES'] if d['DEVICE'] == 'XYStage'][0]
        dist = np.sqrt(((np.array(current_pos) - np.array([xystage['X'], xystage['Y']]))**2).sum())
        dists.append(dist)
        
    ind = np.argmin(dists)
    well_id, site_num = parse_hcs_site_label(position_list['POSITIONS'][ind]['LABEL'])
    print('Nearest position is in well %s (ind = %d and distance = %d)' % (well_id, ind, min(dists)))



class StageVisitationManager:

    def __init__(self, well_ids_to_visit, position_list, mms, mmc):
        self.well_ids_to_visit = well_ids_to_visit
        self.position_list = position_list
        self.mmc = mmc
        self.mms = mms

        # generate the list of well_ids to visit and consume (via .pop())
        self.unvisited_well_ids = self.well_ids_to_visit[::-1]

        # initialize a dict, keyed by well_id, of the measured FocusDrive positions
        self.measured_focusdrive_positions = {}


    def go_to_next_well(self):
        '''
        go to the next well in the well_id list
        '''
        self.current_well_id = self.unvisited_well_ids.pop()
        ind = self.well_id_to_position_ind(self.current_well_id)
        print('Going to well %s' % self.current_well_id)

        try:
            operations.go_to_position(self.mms, self.mmc, ind)
        except py4j.protocol.Py4JJavaError:
            operations.go_to_position(self.mms, self.mmc, ind)    
        print('Arrived at well %s' % self.current_well_id)


    def well_id_to_position_ind(self, well_id):
        '''
        find the index of the first position in a given well
        '''
        for ind, p in enumerate(self.position_list['POSITIONS']):
            if p['LABEL'].startswith(well_id):
                break
        return ind
        

    def call_afc(self):
        '''
        call AFC (if it is in-range) and insert the updated FocusDrive position
        in the list of measured focusdrive positions
        '''
        print('Attempting to call AFC at well %s' % self.current_well_id)

        pos_before = self.mmc.getPosition('FocusDrive')
        self.mmc.fullFocus()
        pos_after = self.mmc.getPosition('FocusDrive')

        self.measured_focusdrive_positions[self.current_well_id] = pos_after
        print('FocusDrive position before AFC: %s' % pos_before)
        print('FocusDrive position after AFC: %s' % pos_after)



def _interp2d_interpolator(positions):
    '''
    Interpolate using piecewise linear interpolation

    Note that this method requires at least one internal (non-edge) position
    to work correctly
    '''
    interpolator = interpolate.interp2d(
        positions[:, 0], 
        positions[:, 1], 
        positions[:, 2], 
        kind='linear')
    return interpolator


def _least_squares_interpolator(positions):
    '''
    Interpolate using least-squares fit

    This is appropriate for small regions for which it is not practical/possible
    to measure the FocusDrive position at internal (non-edge) wells
    '''
    A = np.vstack(
        [positions[:, 0], positions[:, 1], np.ones(positions.shape[0])]).T

    # these are the z-positions we want to interpolate
    y = positions[:, 2]

    # this is the least-squares solution
    p, _, _, _ = np.linalg.lstsq(A, y, rcond=None)

    # this method crudely mimics the behavior of interp2d.__call__
    def interpolator(x, y):
        x = np.atleast_1d(x)
        y = np.atleast_1d(y)
        Z = np.zeros((len(x), len(y)))
        for row_ind in range(Z.shape[0]):
            for col_ind in range(Z.shape[1]):
                Z[row_ind, col_ind] = x[col_ind]*p[0] + y[row_ind]*p[1] + p[2]
        return Z

    return interpolator


def preview_interpolation(
    measured_focusdrive_positions, 
    top_left_well_id, 
    bottom_right_well_id,
    method):
    '''
    '''

    positions = []
    for well_id, zpos in measured_focusdrive_positions.items():
        positions.append((*well_id_to_position(well_id), zpos))
    positions = np.array(positions)

    if method == 'interp2d':
        interpolator = _interp2d_interpolator(positions)
    elif method == 'least-squares':
        interpolator = _least_squares_interpolator(positions)

    top_left_x, top_left_y = well_id_to_position(top_left_well_id)
    bot_right_x, bot_right_y = well_id_to_position(bottom_right_well_id)

    x = np.linspace(top_left_x, bot_right_x, 50)
    y = np.linspace(top_left_y, bot_right_y, 50)

    X, Y = np.meshgrid(x, y)
    Z = interpolator(x, y)

    fig = plt.figure()
    ax = plt.axes(projection='3d')

    ax.plot_surface(
        X, Y, Z, rstride=1, cstride=1,
        cmap='viridis', edgecolor='none')

    ax.scatter3D(positions[:, 0], positions[:, 1], positions[:, 2], color='red')


def interpolate_focusdrive_positions_from_all(
    position_list_filepath, 
    measured_focusdrive_positions, 
    top_left_well_id,
    bottom_right_well_id,
    method=None,
    offset=0):
    '''

    Parameters
    ----------
    position_list_filepath: str
        Local path to a JSON list of positions assumed to have been generated
        by the HCS Site Generator plugin
    measured_focusdrive_positions : a dict of well_ids and measured FocusDrive positions
        e.g., {'B9': 7600, 'B5': 7500, ...}
    top_left_well_id : the well_id of the top-left-most well (usually 'B2')
    bottom_right_well_id : the well_id of the bottom-left-most well (usually 'G9')
    offset : a constant offset (in microns) to add to the interpolated positions

    '''

    # create an array of numeric (x,y,z) positions from the well_ids
    positions = []
    for well_id, zpos in measured_focusdrive_positions.items():
        positions.append((*well_id_to_position(well_id), zpos))
    positions = np.array(positions)

    if method == 'interp2d':
        interpolator = _interp2d_interpolator(positions)
    elif method == 'least-squares':
        interpolator = _least_squares_interpolator(positions)

    with open(position_list_filepath) as file:
        position_list = json.load(file)

    for ind, pos in enumerate(position_list['POSITIONS']):
        
        well_id, site_num = parse_hcs_site_label(pos['LABEL'])
        x, y = well_id_to_position(well_id)

        # the interpolated z-position of the current well
        interpolated_position = interpolator(x, y)[0] + offset

        # the config entry for the 'FocusDrive' device (this is the motorized z-stage)
        focusdrive_config = {
            'X': interpolated_position,
            'Y': 0,
            'Z': 0,
            'AXES': 1,
            'DEVICE': 'FocusDrive',
        }

        position_list['POSITIONS'][ind]['DEVICES'].append(focusdrive_config)
    
    # save the new position_list
    ext = position_list_filepath.split('.')[-1]
    new_filepath = re.sub('.%s$' % ext, '_interpolated_from_all.%s' % ext, position_list_filepath)
    with open(new_filepath, 'w') as file:
        json.dump(position_list, file)

    return new_filepath, position_list



def visualize_interpolation(measured_focusdrive_positions, new_position_list):

    def xyz_from_pos(pos):
        '''
        '''
        well_id, site_num = parse_hcs_site_label(pos['LABEL'])
        focusdrive = [d for d in pos['DEVICES'] if d['DEVICE']=='FocusDrive'][0]
        x, y = well_id_to_position(well_id)
        z = focusdrive['X']
        return x, y, z

    measured_positions = np.array([
        (*well_id_to_position(well_id), zpos) 
            for well_id, zpos in measured_focusdrive_positions.items()])

    pos = np.array([xyz_from_pos(p) for p in new_position_list['POSITIONS']])

    plt.figure()
    ax = plt.axes(projection='3d')

    ax.scatter3D(pos[:, 0], pos[:, 1], pos[:, 2], color='gray')

    ax.scatter3D(
        measured_positions[:, 0], 
        measured_positions[:, 1], 
        measured_positions[:, 2], 
        color='red')