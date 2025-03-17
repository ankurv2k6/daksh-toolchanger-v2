# Automatic Dock position location for DakshV2

from kinematics import cartesian, corexy


class DockCalibrate:
	def __init__(self, config):
		self.config = config
		self.printer = config.get_printer()
		self.name = config.get_name()
		self.xy_resolution = config.getfloat('xy_resolution') # no longer needed, leaving this in so the script doesn't error for existing users
		self.x_resolution = None
		self.y_resolution = None
		self.dock_extra_offset_x_unlock = config.getfloat('dock_extra_offset_x_unlock')
		self.dock_extra_offset_y_unlock = config.getfloat('dock_extra_offset_y_unlock')
		self.dock_extra_offset_x_lock = config.getfloat('dock_extra_offset_x_lock')
		self.dock_extra_offset_y_lock = config.getfloat('dock_extra_offset_y_lock')
		self.gcode = self.printer.lookup_object('gcode')
		gcode_macro = self.printer.load_object(config, 'gcode_macro')
		# G-Code macros
		self.dock_calibrate_unlock_template = gcode_macro.load_template(config, 'dock_calibrate_move_1_gcode', '')
		self.dock_calibrate_home_template = gcode_macro.load_template(config, 'dock_calibrate_move_2_gcode', '')
		self.dock_test_template = gcode_macro.load_template(config, 'dock_test_gcode', '')
		self.rod_install_msg_template = gcode_macro.load_template(config, 'rod_install_msg_gcode', '')


		self.gcode.register_command('CALC_DOCK_LOCATION', self.cmd_CALC_DOCK_LOCATION,
		desc=self.cmd_CALC_DOCK_LOCATION_help)

		self.gcode.register_command('DOCK_TEST', self.cmd_DOCK_TEST,
		desc=self.cmd_DOCK_TEST_help)

	def get_status(self, eventtime):
		return {}

	def get_printer_type(self):
		try:
			toolhead = self.printer.lookup_object('toolhead')
			kin = toolhead.get_kinematics()
			steppers = kin.get_steppers()
			self.console(f"stepx: {steppers[0].get_step_dist()}, stepy:{steppers[1].get_step_dist()}")
			self.x_resolution = steppers[0].get_step_dist()
			self.y_resolution = steppers[1].get_step_dist()
			kinematics_type = str(type(kin))
			if isinstance(kin, cartesian.CartKinematics):
				self.console("this is a cartesian printer")
				return "cartesian"
			elif isinstance(kin, corexy.CoreXYKinematics):
				self.console("this is a corexy printer")
				return "corexy"
			else:
				self.console(f"Unsupported kinematics type: {kinematics_type}. Cannot continue")

		except Exception as e:
			self.console(f"Error: {e}")
		return None

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

	def get_toolhead_position(self):
		toolhead = self.printer.lookup_object('toolhead')
		pos = toolhead.get_position()
		return pos

	def console(self, msg):
		self.gcode.run_script_from_command(f"RESPOND TYPE=command MSG='{msg}'")
		self.gcode.run_script_from_command(f"M117 {msg}")

	cmd_CALC_DOCK_LOCATION_help = "Automatically Calculate Dock Location for Selected Tool"
	def cmd_CALC_DOCK_LOCATION(self, gcmd):
		printer_type = self.get_printer_type()
		if not printer_type:
			gcmd.respond_info("ERROR: this printer kinematics type is not supported. Canceling calibration")
			return
		tool = gcmd.get("TOOL")
		toolhead = self.printer.lookup_object('toolhead')

		#get starting position, this is tool docked
		self.console("Starting dock calibration")
		initial_res = self.get_mcu_position();

		#run first move to unlock the tool
		self.console("Running first Gcode move: Moving to unlock the tool")
		self.dock_calibrate_unlock_template.run_gcode_from_command()
		self.gcode.run_script_from_command('G4 P2000')
		unlock_res = self.get_mcu_position();

		#run second move to home the axes
		self.console("Running second Gcode move: Homing axes")
		self.dock_calibrate_home_template.run_gcode_from_command()
		self.gcode.run_script_from_command('G4 P2000')
		home_res = self.get_mcu_position();

		real_pos = self.get_toolhead_position() # returns position array [X,Y,Z,E]
		self.console(f"real_pos: {real_pos[0]}, {real_pos[1]}, {real_pos[2]}")
		if printer_type == "cartesian":
			dx2 = home_res['x'] - unlock_res['x']
			dy2 = home_res['y'] - unlock_res['y']

			unlock_x = real_pos[0]-(dx2 * self.x_resolution) + self.dock_extra_offset_x_unlock
			unlock_y = real_pos[1]-(dy2 * self.y_resolution) + self.dock_extra_offset_y_unlock

			dx1 = home_res['x'] - initial_res['x']
			dy1 = home_res['y'] - initial_res['y']

			lock_x = real_pos[0]-(dx1 * self.x_resolution) + self.dock_extra_offset_x_lock
			lock_y = real_pos[1]-(dy1 * self.y_resolution) + self.dock_extra_offset_y_lock

		elif printer_type == "corexy":
			# get unlock coordinates
			dx2 = home_res['x'] - unlock_res['x']
			dy2 = home_res['y'] - unlock_res['y']
			unlock_move_diff_x = (dx2*self.x_resolution + dy2*self.y_resolution)/2 
			unlock_move_diff_y = (dx2*self.x_resolution - dy2*self.y_resolution)/2 
			unlock_x = real_pos[0]-unlock_move_diff_x + self.dock_extra_offset_x_unlock
			unlock_y = real_pos[1]-unlock_move_diff_y + self.dock_extra_offset_y_unlock

			# get docked coordinates
			dx1 = home_res['x'] - initial_res['x']
			dy1 = home_res['y'] - initial_res['y']

			move_diff_x = (dx1*self.x_resolution + dy1*self.y_resolution)/2 
			move_diff_y = (dx1*self.x_resolution - dy1*self.y_resolution)/2 

			lock_x = real_pos[0]-move_diff_x + self.dock_extra_offset_x_lock
			lock_y = real_pos[1]-move_diff_y + self.dock_extra_offset_y_lock
		else:
			return

		lock_x = round(lock_x, 2)
		lock_y = round(lock_y, 2)
		unlock_x = round(unlock_x, 2)
		unlock_y = round(unlock_y, 2)

		gcmd.respond_info(f"Calculated dock location: X{lock_x}, Y{lock_y}")	

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
