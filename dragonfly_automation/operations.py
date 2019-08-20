
import numpy as np


def log_operation(operation):

    def wrapper(*args, **kwargs):
        print('START OPERATION: %s' % operation.__name__)
        result = operation(*args, **kwargs)
        print('END OPERATION: %s' % operation.__name__)
        return result

    return wrapper


@log_operation
def autofocus(mm_studio, mm_core, channel_settings):

    '''
    Autofocus using a given configuration

    Parameters
    ----------
    mm_studio, mm_core : 
    channel_settings : 

    TODO: optionally specify the autofocus method (either AFC or traditional autofocus)
    
    '''

    # change to the right channel
    change_channel(mm_core, channel_settings)

    af_manager = mm_studio.getAutofocusManager()
    af_plugin = af_manager.getAutofocusMethod()

    try:
        af_plugin.fullFocus()
    except Exception as error:
        print('WARNING: AFC failure')
        print(error)

    # just to be safe
    mm_core.waitForSystem()


def acquire_snap(gate, mm_studio):
    '''
    Acquire an image using the current laser/camera/exposure settings
    and return the image data as a numpy memmap

    TODO: call gate.clearQueue (wait for new version of mm2python)
    TODO: check that meta.bitDepth is uint16
    '''

    mm_studio.live().snap(True)
    meta = gate.getLastMeta()

    data = np.memmap(
        meta.getFilepath(),
        dtype='uint16',
        mode='r+',
        offset=0,
        shape=(meta.getxRange(), meta.getyRange()))
    
    return data


@log_operation
def acquire_stack(mm_core, datastore, settings):
    '''
    Acquire a z-stack using the given settings
    and 'put' it in the datastore object
    '''

    pass


@log_operation
def change_channel(mm_core, channel_settings):
    '''
    Convenience method to set the laser power, exposure time, and camera gain
    
    (KC: pretty sure the order of these operations doesn't matter,
    but to be safe the order here is preserved from Nathan's script)
    '''

    # hardware config
    mm_core.setConfig(
        channel_settings.config_group, 
        channel_settings.config_name)

    # TODO: is this waitForConfig call necessary?
    mm_core.waitForConfig(
        channel_settings.config_group, 
        channel_settings.config_name)

    # laser power
    mm_core.setProperty(
        channel_settings.laser_line,
        channel_settings.laser_name,
        channel_settings.laser_power)

    # exposure time
    mm_core.setExposure(
        float(channel_settings.exposure_time))

    # camera gain
    prop_name = 'Gain'
    mm_core.setProperty(
        channel_settings.camera_name, 
        prop_name, 
        channel_settings.camera_gain)


def move_z(mm_core, stage_label, position=None, kind=None):
    '''
    Convenience method to move a z-stage
    (adapted from Nathan's script)

    TODO: basic sanity checks on the value of `position`
    (e.g., if kind=='relative', `position` shouldn't be a 'big' number)
    '''

    # validate `kind`
    if kind not in ['relative', 'absolute']:
        raise ValueError("`kind` must be either 'relative' or 'absolute'")
    
    # validate `position`
    try:
        position = float(position)
    except ValueError:
        raise TypeError('`position` cannot be coerced to float')
    
    if np.isnan(position):
        raise TypeError('`position` cannot be nan')
    
    # move the stage
    if kind=='absolute':
        mm_core.setPosition(stage_label, position)
    elif kind=='relative':
        mm_core.setRelativePosition(stage_label, position)
    
    # return the actual position of the stage
    mm_core.waitForDevice(stage_label)
    actual_position = mm_core.getPosition(stage_label)
    return actual_position


@log_operation
def autoexposure(
    gate,
    mm_studio,
    mm_core,
    stack_settings, 
    channel_settings,
    autoexposure_settings):
    '''

    Parameters
    ----------
    gate, mm_studio, mm_core : gateway objects
    stack_settings : an instance of StackSettings
    channel_settings : the settings object for the channel to auto-exposure 
    autoexposure_settings : an instance of AutoexposureSettings


    Returns
    -------

    laser_power  : float
        The calculated laser power

    exposure_time : float
        The calculated exposure time

    autoexposure_did_succeed : bool
        Whether the autoexposure algorithm was successful


    Algorithm description
    ---------------------
    slice check:
    while an over-exposed slice exists:
      step through z-stack until an over-exposed slice is encountered
      lower exposure time and/or laser power

    stack check:
    one-time check for under-exposure using the overall max intensity;
    increase the exposure time, up to the maximum, if necessary

    '''
    
    autoexposure_did_succeed = True

    # move to the bottom of the z-stack
    current_z_position = move_z(
        mm_core, 
        stack_settings.stage_label, 
        position=stack_settings.relative_bottom, 
        kind='relative')

    while current_z_position <= stack_settings.relative_top:

        # snap an image
        mm_core.waitForSystem()
        snap_data = acquire_snap(gate, mm_studio)

        # check the exposure
        laser_power, exposure_time, slice_was_overexposed = check_slice_for_overexposure(
            channel_settings.laser_power, 
            channel_settings.exposure_time, 
            snap_data,
            autoexposure_settings)

        # if over-exposed, update the laser power and exposure time and reset the z-stack
        if slice_was_overexposed:
            print('z-slice at %s was overexposed' % current_z_position)
            
            channel_settings.laser_power = laser_power
            channel_settings.exposure_time = exposure_time

            # update laser power
            mm_core.setProperty(
                channel_settings.laser_line,
                channel_settings.laser_name,
                channel_settings.laser_power)

            # update exposure time
            mm_core.setExposure(
                float(channel_settings.exposure_time))

            # reset the z-stack
            new_z_position = stack_settings.relative_bottom

        else:
            print('z-slice at %s was not overexposed' % current_z_position)
            new_z_position = current_z_position + stack_settings.step_size

        # move to the new z-position 
        # (either the next slice or back to the bottom of the stack)
        current_z_position = move_z(
            mm_core, 
            stack_settings.stage_label, 
            position=new_z_position,
            kind='absolute')
    
    return autoexposure_did_succeed



def check_slice_for_overexposure(laser_power, exposure_time, snap_data, autoexposure_settings):
    '''
    Check a single slice for over-exposure, 
    and lower the exposure time or laser power if it is indeed over-exposed

    Parameters
    ----------
    laser_power : the laser power used to acquire the snap
    exposure_time : the exposure time used to acquire the snap
    snap_data : the slice image data as a numpy array (assumed to be uint16)
    autoexposure_settings : an instance of AutoexposureSettings (with min/max/default exposure times etc)
    '''

    # KC: the 99.9th percentile was empirically determined;
    # it corresponds to ~1000 pixels in a 1024x1024 image
    slice_was_overexposed = np.percentile(snap_data, 99.9) > autoexposure_settings.max_intensity
    if slice_was_overexposed:
        exposure_time *= autoexposure_settings.relative_exposure_step

        # if the exposure time is now below the minimum, turn down the laser instead
        if exposure_time < autoexposure_settings.min_exposure_time:
            exposure_time = autoexposure_settings.default_exposure_time
            laser_power *= autoexposure_settings.relative_exposure_step

    return laser_power, exposure_time, slice_was_overexposed