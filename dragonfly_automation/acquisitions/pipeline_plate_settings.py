
'''
Settings for the 'pipeline_plate' program

NOTE: exposure times and camera gain values must be floats
TODO: should/must laser powers also be floats? (they are ints in Nathan's script)

'''

from dragonfly_automation.settings_schemas import (
    StackSettings,
    ChannelSettings,
    AutoexposureSettings,
)


# -----------------------------------------------------------------------------
#
# z-stack range, relative to the AFC point, and step size
#
# -----------------------------------------------------------------------------
STAGE_LABEL = 'PiezoZ'
dev_stack_settings = StackSettings(
    stage_label=STAGE_LABEL,
    relative_top=16.0,
    relative_bottom=-10.0,
    step_size=7.0
)

prod_stack_settings = StackSettings(
    stage_label=STAGE_LABEL,
    relative_top=16.0,
    relative_bottom=-6.0,
    step_size=0.2
)

# brightfield stack settings (with wider range and coarser step size)
bf_stack_settings = StackSettings(
    stage_label=STAGE_LABEL,
    relative_top=20.0,
    relative_bottom=-20.0,
    step_size=1.0
)


# -----------------------------------------------------------------------------
#
# Channel settings for DAPI and GFP
#
# -----------------------------------------------------------------------------
# common names and settings shared between channels
CONFIG_GROUP = 'Channels-EMCCD'
LASER_LINE = 'Andor ILE-A'
CAMERA_NAME = 'Andor EMCCD'
DEFAULT_LASER_POWER = 10
DEFAULT_CAMERA_GAIN = 400.0
DEFAULT_EXPOSURE_TIME = 50.0

dapi_channel_settings = ChannelSettings(
    config_group=CONFIG_GROUP,
    config_name='EMCCD_Confocal40_DAPI',
    camera_name=CAMERA_NAME,
    laser_line=LASER_LINE,
    laser_name='Laser 405-Power Setpoint',
    default_laser_power=DEFAULT_LASER_POWER,
    default_exposure_time=DEFAULT_EXPOSURE_TIME,
    default_camera_gain=DEFAULT_CAMERA_GAIN
)

gfp_channel_settings = ChannelSettings(
    config_group=CONFIG_GROUP,
    config_name='EMCCD_Confocal40_GFP',
    camera_name=CAMERA_NAME,
    laser_line=LASER_LINE,
    laser_name='Laser 488-Power Setpoint',
    default_laser_power=DEFAULT_LASER_POWER,
    default_exposure_time=DEFAULT_EXPOSURE_TIME,
    default_camera_gain=DEFAULT_CAMERA_GAIN
)

# provisional brightfield settings
# TODO: check that the config name is the right one
bf_channel_settings = ChannelSettings(
    config_group=CONFIG_GROUP,
    config_name='EMCCD_BF',
    camera_name=CAMERA_NAME,
    laser_line=None,
    laser_name=None,
    default_laser_power=None,
    default_exposure_time=100.0,
    default_camera_gain=400.0
)


# -----------------------------------------------------------------------------
#
# Autoexposure settings
#
# -----------------------------------------------------------------------------
autoexposure_settings = AutoexposureSettings(

    # min intensity defines under-exposure;
    # chosen to be about 20x the background (or readout noise) when the EM gain is 400
    min_intensity=2**14,

    # max intensity defines over-exposure; 
    # chosen to leave a (literal) bit of wiggle room
    max_intensity=2**15,

    # min and max exposure times 
    # (min of 40ms is to avoid artifacts from the spinning disk)
    min_exposure_time=40.0,
    max_exposure_time=500.0,
    default_exposure_time=DEFAULT_EXPOSURE_TIME,

    # min laser power is just a guess
    min_laser_power=1,

    # exposure step (for over-exposure) is from Nathan
    relative_exposure_step=0.8,

    # a coarse z-step is sufficient for stepping through the stack during autoexposure
    z_step_size=1.0
)

