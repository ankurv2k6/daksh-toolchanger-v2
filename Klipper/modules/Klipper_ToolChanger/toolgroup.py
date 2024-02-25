# KTCC - Klipper Tool Changer Code
# Toolgroup module, used to group Tools and derived from Tool.
#
# Copyright (C) 2023  Andrei Ignat <andrei@ignat.se>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# To try to keep terms apart:
# Mount: Tool is selected and loaded for use, be it a physical or a virtual on physical.
# Unmopunt: Tool is unselected and unloaded, be it a physical or a virtual on physical.
# Pickup: Tool is physically picked up and attached to the toolchanger head.
# Droppoff: Tool is physically parked and dropped of the toolchanger head.
# ToolLock: Toollock is engaged.
# ToolUnLock: Toollock is disengaged.


class ToolGroup:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split(' ')[1]
        self.config = config
        # gcode_macro = self.printer.load_object(config, 'gcode_macro')

        try:
            _, name = config.get_name().split(' ', 1)
            self.name = int(name)
        except ValueError:
            raise config.error(
                    "Name of section '%s' contains illegal characters. Use only integer ToolGroup number."
                    % (config.get_name()))

        self.is_virtual = config.getboolean(    # If True then must have a physical_parent declared and shares extruder, hotend and fan with the physical_parent
            'is_virtual', False)
        self.physical_parent_id = config.getint(   # Tool used as a Physical parent for all toos of this group. Only used if the tool i virtual.
            'physical_parent', None)
        self.lazy_home_when_parking = config.get('lazy_home_when_parking', 0)    # (default: 0) - When set to 1, will home unhomed XY axes if needed and will not move any axis if already homed and parked. 2 Will also home Z if not homed.
       # -1 = none, 1= Only load filament, 2= Wipe in front of carriage, 3= Pebble wiper, 4= First Silicone, then pebble. Defaults to 0.
        self.pickup_gcode = config.get('pickup_gcode', '')
        self.dropoff_gcode = config.get('dropoff_gcode', '')
        self.virtual_toolload_gcode = config.get('virtual_toolload_gcode', '')
        self.virtual_toolunload_gcode = config.get('virtual_toolunload_gcode', '')
        self.meltzonelength = config.get('meltzonelength', 0)
        self.idle_to_standby_time = config.getfloat( 'idle_to_standby_time', 30, minval = 0.1)
        self.idle_to_powerdown_time = config.getfloat( 'idle_to_powerdown_time', 600, minval = 0.1)

        self.requires_pickup_for_virtual_load = self.config.getboolean("requires_pickup_for_virtual_load", True)
        self.requires_pickup_for_virtual_unload = self.config.getboolean("requires_pickup_for_virtual_unload", True)
        self.unload_virtual_at_dropoff = self.config.getboolean("unload_virtual_at_dropoff", True)


    def get_config(self, config_param, default = None):
        return self.config.get(config_param, default)
        
    def get_status(self, eventtime= None):
        status = {
            "is_virtual": self.is_virtual,
            "physical_parent_id": self.physical_parent_id,
            "lazy_home_when_parking": self.lazy_home_when_parking,
            "meltzonelength": self.meltzonelength,
            "idle_to_standby_time": self.idle_to_standby_time,
            "idle_to_powerdown_time": self.idle_to_powerdown_time,
            "requires_pickup_for_virtual_load": self.requires_pickup_for_virtual_load,
            "requires_pickup_for_virtual_unload": self.requires_pickup_for_virtual_unload,
            "unload_virtual_at_dropoff": self.unload_virtual_at_dropoff
        }
        return status

def load_config_prefix(config):
    return ToolGroup(config)




