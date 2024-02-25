# rounded paths for fast travel.
#
# Copyright (C) 2023  Viesturs Zarins <viesturz@gmail.com>

# Aimed to optimize travel paths by minimizing speed changes for sharp corners.
# Supports arbitrary paths in XYZ.
# Each corner is rounded to a maximum deviation distance of D.
# Since each corner depends on the next one, the chain needs to end with an R=0
# command to flush pending moves.
# Coordinates created by this are converted into G0 commands.

# This file may be distributed under the terms of the GNU GPLv3 license.
import math
EPSILON = 0.001
EPSILON_ANGLE = 0.001

class ControlPoint:
	def __init__(self, x, y, z, d, f):
		self.vec = [x,y,z]
		self.f = f
		self.maxd = d
		self.angle = 0.0
		self.len = 0.0 # distance to the previous point
		# max distance of the rounding from the corner based on D and angle.
		self.lin_d = 0.0
		self.lin_d_to_r = 0.0

# Some basic vector math
def _vecto(f: ControlPoint, t: ControlPoint)->list:
	return [t.vec[i]-f.vec[i] for i in range(3)]

def _vadd(f: list, t: list) ->list:
	return [f[i]+ t[i] for i in range(3)]

def _vmul(f:list, n) ->list:
	return [f[i] * n for i in range(3)]

def _cross(vp: list, vn: list) -> list:
	return [vp[1] * vn[2] - vp[2] * vn[1], vp[2] * vn[0] - vp[0] * vn[2],
		   vp[0] * vn[1] - vp[1] * vn[0]]

def _vdist(v0: list, v1:list) -> float:
	return math.hypot(v0[0]-v1[0], v0[1]-v1[1], v0[2]-v1[2])

def _vnorm(vec: list) -> list:
	invlen = 1.0/math.hypot(*vec)
	return [x*invlen for x in vec]

def _vangle(vec1: list, vec2: list) -> float:
	crossx = vec1[1] * vec2[2] - vec1[2] * vec2[1]
	crossy = vec1[2] * vec2[0] - vec1[0] * vec2[2]
	crossz = vec1[0] * vec2[1] - vec1[1] * vec2[0]
	cross = math.hypot(crossx, crossy, crossz)
	dot = vec1[0] * vec2[0] + vec1[1] * vec2[1] + vec1[2] * vec2[2]
	return math.atan2(cross, dot)

def _vrot(vec: list, angle, axis: list) -> list:
	# Axis needs to be normalized
	# https://en.wikipedia.org/wiki/Rotation_matrix
	s = math.sin(angle)
	c = math.cos(angle)
	t = 1 - c
	return [
		vec[0] * (t * axis[0] ** 2 + c) + vec[1] * (t * axis[0] * axis[1] - s * axis[2]) + vec[2] * (t * axis[0] * axis[2] + s * axis[1]),
		vec[0] * (t * axis[0] * axis[1] + s * axis[2]) + vec[1] * (t * axis[1] ** 2 + c) + vec[2] * (t * axis[1] * axis[2] - s * axis[0]),
		vec[0] * (t * axis[0] * axis[2] - s * axis[1]) + vec[1] * (t * axis[1] * axis[2] + s * axis[0]) + vec[2] * (t * axis[2] ** 2 + c)
	]

def _vrot_transform(angle: float, axis: list) -> list:
	# Axis needs to be normalized
	# https://en.wikipedia.org/wiki/Rotation_matrix
	s = math.sin(angle)
	c = math.cos(angle)
	t = 1 - c
	return [(t * axis[0] ** 2 + c), (t * axis[0] * axis[1] - s * axis[2]), (t * axis[0] * axis[2] + s * axis[1]),
		(t * axis[0] * axis[1] + s * axis[2]), (t * axis[1] ** 2 + c), (t * axis[1] * axis[2] - s * axis[0]),
		(t * axis[0] * axis[2] - s * axis[1]), (t * axis[1] * axis[2] + s * axis[0]), (t * axis[2] ** 2 + c)]

def _vtransform(vec: list, transform: list) -> list:
	return [vec[0] * transform[0] + vec[1]*transform[1] + vec[2]* transform[2],
			vec[0] * transform[3] + vec[1] * transform[4] + vec[2] * transform[5],
			vec[0] * transform[6] + vec[1] * transform[7] + vec[2] * transform[8]]

class RoundedPath:
	buffer: list[ControlPoint]

	def __init__(self, config):
		self.printer = config.get_printer()
		self.mm_per_arc_segment = config.getfloat('resolution', 1., above=0.0)

		self.gcode_move = self.printer.load_object(config, 'gcode_move')
		self.gcode = self.printer.lookup_object('gcode')
		self.G0_params = {}
		self.G0_cmd = self.gcode.create_gcode_command("G0", "G0", self.G0_params)
		self.real_G0 = self.gcode_move.cmd_G1
		self.gcode.register_command("ROUNDED_G0", self.cmd_ROUNDED_G0)
		self.buffer = []
		self.lastg0 = []

		if config.getboolean('replace_g0', False):
			self.gcode.register_command("G0", None)
			self.gcode.register_command("G0", self.cmd_ROUNDED_G0)

	def cmd_ROUNDED_G0(self, gcmd):
		d = gcmd.get_float("D", 0.0)
		if d <= 0.0 and len(self.buffer) < 2:
			self.real_G0(gcmd)
			return
		gcodestatus = self.gcode_move.get_status()
		if not gcodestatus['absolute_coordinates']:
			raise gcmd.error("ROUNDED_G0 does not support relative move mode")
		currentPos = gcodestatus['gcode_position']
		if len(self.buffer) == 0:
			# Initialize with currentPos and radius = 0.
			self.buffer.append(ControlPoint(x= currentPos[0], y= currentPos[1], z=currentPos[2],d =0.0, f = 0.0))
		else:
			origin = self.buffer[0].vec
			if _vdist(currentPos, origin) > EPSILON:
				raise gcmd.error("ROUNDED_G0 - current position changed since previous command, the last ROUNDED_G0 before other moves needs to be with D=0")
			last = self.buffer[-1]
			currentPos = last.vec

		self._lineto(ControlPoint(x = gcmd.get_float("X", currentPos[0]),
								  y = gcmd.get_float("Y", currentPos[1]),
								  z = gcmd.get_float("Z", currentPos[2]),
								  f = gcmd.get_float("F", 0.0),
								  d = d))

	def _lineto(self, pos):
		self.buffer.append(pos)
		if len(self.buffer) >= 3:
			self._calculate_corner(self.buffer[-2], self.buffer[-3], self.buffer[-1])

		if len(self.buffer) >= 2 and self.buffer[-1].maxd <= 0.0:
			self._calculate_zero_corner(self.buffer[-1], self.buffer[-2])
			# zero max offset, flush everything.
			self._flush_buffer(len(self.buffer) -2)
			self._g0(self.buffer[-1])
			self.buffer.clear()
		elif len(self.buffer) >= 4 and self.buffer[-3].lin_d + self.buffer[-2].lin_d <= self.buffer[-2].len:
			# max offsets don't overlap, flush everything, but the last segment.
			self._flush_buffer(len(self.buffer) - 3)

	# Computes the max curve start offset along the edge based on max distance.
	def _calculate_corner(self, c:ControlPoint, v1:ControlPoint, v2:ControlPoint):
		vec1 = _vecto(c, v1)
		vec2 = _vecto(c, v2)
		c.len = math.hypot(*vec1)
		c.angle = _vangle(vec1, vec2)
		if abs(c.angle) < EPSILON_ANGLE or math.pi - abs(c.angle) < EPSILON_ANGLE:
			# too close of an angle - do not bother
			return
		sina2 = math.sin(c.angle / 2)
		tana2 = math.tan(c.angle/2)
		radius = c.maxd * sina2 / (1-sina2)
		c.lin_d_to_r = tana2
		c.lin_d = radius/tana2

	def _calculate_zero_corner(self, c:ControlPoint, vp:ControlPoint):
		vec1 = _vecto(c, vp)
		c.len = math.hypot(*vec1)
		c.angle = 0

	def _flush_buffer(self, num_segments):
		if num_segments <= 0:
			return
		if len(self.buffer) < 2:
			self.buffer.clear()
			return
		if len(self.buffer) == 2:
			self._g0(self.buffer[-1])
			self.buffer.clear()
			return

		self._deconflict_lin_d(num_segments+1)

		for i in range(num_segments):
			self._arc(self.buffer[i+1], self.buffer[i],self.buffer[i+2])

		self.buffer = self.buffer[num_segments:]
		# Update where we finished
		self.buffer[0].vec = self.lastg0

	def _deconflict_lin_d(self, num_segments):
		order = [i+1 for i in range(num_segments)]
		order = sorted(order, key=lambda a: self.buffer[a].len)
		# Process segments, shortest first
		for i in order:
			p0 = self.buffer[i-1]
			p1 = self.buffer[i]
			missingd = p1.lin_d + p0.lin_d - p1.len
			if missingd <= 0:
				continue

			# first try to reduce the biggest radius
			r0 = p0.lin_d * p0.lin_d_to_r
			r1 = p1.lin_d * p1.lin_d_to_r
			if r0 > r1:
				missingr0 = missingd * p0.lin_d_to_r + EPSILON
				r0 = max(r1, r0 - missingr0)
				p0.lin_d = r0 / p0.lin_d_to_r
			elif r1 > r0:
				missingr1 = missingd * p1.lin_d_to_r + EPSILON
				r1 = max(r0, r1 - missingr1)
				p1.lin_d = r1 / p1.lin_d_to_r
			missingd = p1.lin_d + p0.lin_d - p1.len
			if missingd <= 0:
				continue
			if p0.lin_d_to_r <= 0.0 or p1.lin_d_to_r <= 0.0:
				# should never happen, just to be safe, floating points are tricky
				p0.lin_d = 0
				p1.lin_d = 0
				continue
			# that was not enough, reduce both proportionally
			missingr_shared = missingd / (1/p0.lin_d_to_r + 1/p1.lin_d_to_r)
			p0.lin_d = max(0.0, p0.lin_d - missingr_shared / p0.lin_d_to_r)
			p1.lin_d = max(0.0, p1.lin_d - missingr_shared / p1.lin_d_to_r)

	def _arc(self, c:ControlPoint, p:ControlPoint, n:ControlPoint):
		radius = c.lin_d * c.lin_d_to_r
		num_segments = math.floor(radius * c.angle / self.mm_per_arc_segment)
		if num_segments < 1:
			self._g0(c)
			return
		vp = _vnorm(_vecto(c, p))
		vn = _vnorm(_vecto(c, n))
		rotaxis = _vnorm(_cross(vp, vn))
		start = _vadd(c.vec, _vmul(vp, c.lin_d))
		spoke = _vmul(_vrot(vp, math.pi/2, rotaxis), -radius)
		center = _vadd(start, _vmul(spoke, -1.0))

		# We are rotating counter the segment rotation.
		rot_transform = _vrot_transform(-c.angle / num_segments, rotaxis)
		rotspoke = spoke
		self._g0p(c, _vadd(center, rotspoke))
		for step in range(0, num_segments):
			rotspoke = _vtransform(rotspoke, rot_transform)
			self._g0p(c, _vadd(center, rotspoke))

	def _g0(self, p: ControlPoint):
		self._g0p(p, p.vec)

	def _g0p(self, p: ControlPoint, vec: list):
		self.G0_params["X"]=vec[0]
		self.G0_params["Y"]=vec[1]
		self.G0_params["Z"]=vec[2]
		if p.f > 0.0:
			self.G0_params['F'] = p.f
		else:
			self.G0_params.pop('F', None)
		self.lastg0 = vec
		self.real_G0(self.G0_cmd)

def load_config(config):
	return RoundedPath(config)