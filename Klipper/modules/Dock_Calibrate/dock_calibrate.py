# Nozzle alignment module for 3d kinematic probes.
#
# This module has been adapted from code written by Kevin O'Connor <kevin@koconnor.net> and Martin Hierholzer <martin@hierholzer.info>
# Sourced from https://github.com/ben5459/Klipper_ToolChanger/blob/master/probe_multi_axis.py

import logging


class DockCalibrate:
	def __init__(self, config):
		self.printer = config.get_printer()
		self.name = config.get_name()
		self.xy_resolution = config.getfloat('xy_resolution')
		self.dock_extra_offset_x_unlock = config.getfloat('dock_extra_offset_x_unlock')
		self.dock_extra_offset_y_unlock = config.getfloat('dock_extra_offset_y_unlock')
		self.dock_extra_offset_x_lock = config.getfloat('dock_extra_offset_x_lock')
		self.dock_extra_offset_y_lock = config.getfloat('dock_extra_offset_y_lock')
		self.gcode = self.printer.lookup_object('gcode')
		gcode_macro = self.printer.load_object(config, 'gcode_macro')
		# G-Code macros
		self.dock_calibrate_move_1_template = gcode_macro.load_template(config, 'dock_calibrate_move_1_gcode', '')
		self.dock_calibrate_move_2_template = gcode_macro.load_template(config, 'dock_calibrate_move_2_gcode', '')
		self.dock_test_template = gcode_macro.load_template(config, 'dock_test_gcode', '')
		self.rod_install_msg_template = gcode_macro.load_template(config, 'rod_install_msg_gcode', '')
		
		
		self.gcode.register_command('CALC_DOCK_LOCATION', self.cmd_CALC_DOCK_LOCATION,
		desc=self.cmd_CALC_DOCK_LOCATION_help)

		self.gcode.register_command('DOCK_TEST', self.cmd_DOCK_TEST,
		desc=self.cmd_DOCK_TEST_help)
		
	def get_status(self, eventtime):		
		return {}
				
	def get_mcu_position(self):		
		toolhead = self.printer.lookup_object('toolhead')
		steppers = toolhead.kin.get_steppers()
		for s in steppers:
			if s.get_name() == "stepper_x":
				mcu_pos_x = s.get_mcu_position()
	  	
			if s.get_name() == "stepper_y":
				mcu_pos_y = s.get_mcu_position()
			
		return {'x':mcu_pos_x,
				'y':mcu_pos_y}

	cmd_CALC_DOCK_LOCATION_help = "Automatically Calculate Dock Location for Selected Tool"
	def cmd_CALC_DOCK_LOCATION(self, gcmd):
		tool = gcmd.get("TOOL")
		toolhead = self.printer.lookup_object('toolhead')

		initial_res = self.get_mcu_position();
		logging.info(initial_res)
		self.dock_calibrate_move_1_template.run_gcode_from_command()
		self.gcode.run_script_from_command('G4 P2000')
		move_1_res = self.get_mcu_position();
		logging.info(move_1_res)
		self.dock_calibrate_move_2_template.run_gcode_from_command()
		self.gcode.run_script_from_command('G4 P2000')
		move_2_res = self.get_mcu_position();
		logging.info(move_2_res)
		
		dx2 = move_2_res['x'] - move_1_res['x']
		dy2 = move_2_res['y'] - move_1_res['y']

		unlock_x = -(((dx2 + dy2)/2) * self.xy_resolution) + self.dock_extra_offset_x_unlock
		unlock_y = -(((dx2 - dy2)/2) * self.xy_resolution) + self.dock_extra_offset_y_unlock

		dx1 = move_2_res['x'] - initial_res['x']
		dy1 = move_2_res['y'] - initial_res['y']
		
		
		lock_x = -(((dx1 + dy1)/2) * self.xy_resolution) + self.dock_extra_offset_x_lock
		lock_y = -(((dx1 - dy1)/2) * self.xy_resolution) + self.dock_extra_offset_y_lock
		
	
		save_variables = self.printer.lookup_object('save_variables')

		save_variables.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command("SAVE_VARIABLE", "SAVE_VARIABLE", {"VARIABLE": 't'+tool+'_lock_x', 'VALUE': round(lock_x, 2) }))
		save_variables.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command("SAVE_VARIABLE", "SAVE_VARIABLE", {"VARIABLE": 't'+tool+'_lock_y', 'VALUE': round(lock_y, 2) }))

		save_variables.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command("SAVE_VARIABLE", "SAVE_VARIABLE", {"VARIABLE": 't'+tool+'_unlock_x', 'VALUE': round(unlock_x, 2) }))
		save_variables.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command("SAVE_VARIABLE", "SAVE_VARIABLE", {"VARIABLE": 't'+tool+'_unlock_y', 'VALUE': round(unlock_y, 2) }))
	
			
	cmd_DOCK_TEST_help = "Automatically Calculate Dock Location for Selected Tool"
	def cmd_DOCK_TEST(self, gcmd):
		self.rod_install_msg_template.run_gcode_from_command()
		self.dock_test_template.run_gcode_from_command()
						
def load_config(config):
	return DockCalibrate(config)