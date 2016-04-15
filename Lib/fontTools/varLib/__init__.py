"""Module for dealing with 'gvar'-style font variations,
also known as run-time interpolation."""

from __future__ import print_function, division, absolute_import
from fontTools.misc.py23 import *
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._n_a_m_e import NameRecord
from fontTools.ttLib.tables._f_v_a_r import table__f_v_a_r, Axis, NamedInstance
from fontTools.ttLib.tables._g_l_y_f import table__g_l_y_f, GlyphCoordinates
from fontTools.ttLib.tables._g_v_a_r import table__g_v_a_r, GlyphVariation
import operator
import xml.etree.ElementTree as ET
import os.path

# TODO remove me
from pprint import pprint

#
# Variation space, aka design space, model
#

class VariationModel(object):

	"""
	Locations must be in normalized space.  Ie. base master
	is at origin (0).
	>>> locations = [ \
	{'wght':100}, \
	{'wght':-100}, \
	{'wght':-180}, \
	{'wdth':+.3}, \
	{'wght':+120,'wdth':.3}, \
	{'wght':+120,'wdth':.2}, \
	{}, \
	{'wght':+180,'wdth':.3}, \
	{'wght':+180}, \
	]
	>>> model = VariationModel(locations, axisOrder=['wght'])
	>>> assert model.sortedLocations == \
	[{}, \
	 {u'wght': -100}, \
	 {u'wght': -180}, \
	 {u'wght': 100}, \
	 {u'wght': 180}, \
	 {u'wdth': 0.3}, \
	 {u'wdth': 0.3, u'wght': 180}, \
	 {u'wdth': 0.3, u'wght': 120}, \
	 {u'wdth': 0.2, u'wght': 120}, \
	]
	>>> assert model.deltaWeights == \
	{0: {0: 1.0, 6: -1.0}, \
	 1: {1: 1.0, 6: -1.0}, \
	 2: {2: 1.0, 6: -1.0}, \
	 3: {3: 1.0, 6: -1.0}, \
	 4: {0: -0.25, 3: -1.0, 4: 1.0, 6: -1.0, 7: -0.75, 8: -0.75}, \
	 5: {0: -0.25, \
	     3: -0.33333333333333326, \
	     4: -0.33333333333333326, \
	     5: 1.0, \
	     6: -1.0, \
	     7: -0.24999999999999994, \
	     8: -0.75}, \
	 6: {6: 1.0}, \
	 7: {3: -1.0, 6: -1.0, 7: 1.0, 8: -1.0}, \
	 8: {6: -1.0, 8: 1.0} \
	}
	"""

	def __init__(self, locations, axisOrder=[]):
		locations = [{k:v for k,v in loc.items() if v != 0.} for loc in locations]
		keyFunc = self.getMasterLocationsSortKeyFunc(locations, axisOrder=axisOrder)
		axisPoints = keyFunc.axisPoints
		self.locations = locations
		self.sortedLocations = sorted(locations, key=keyFunc)
		self.mapping = [locations.index(l) for l in self.sortedLocations]
		self.reverseMapping = [self.sortedLocations.index(l) for l in locations]

		self._computeMasterSupports(axisPoints)

	@staticmethod
	def getMasterLocationsSortKeyFunc(locations, axisOrder=[]):
		assert {} in locations, "Base master not found."
		axisPoints = {}
		for loc in locations:
			if len(loc) != 1:
				continue
			axis = next(iter(loc))
			value = loc[axis]
			if axis not in axisPoints:
				axisPoints[axis] = {0}
			assert value not in axisPoints[axis]
			axisPoints[axis].add(value)

		def getKey(axisPoints, axisOrder):
			def sign(v):
				return -1 if v < 0 else +1 if v > 0 else 0
			def key(loc):
				rank = len(loc)
				onPointAxes = [axis for axis,value in loc.items() if value in axisPoints[axis]]
				orderedAxes = [axis for axis in axisOrder if axis in loc]
				orderedAxes.extend([axis for axis in sorted(loc.keys()) if axis not in axisOrder])
				return (
					rank, # First, order by increasing rank
					-len(onPointAxes), # Next, by decreasing number of onPoint axes
					tuple(axisOrder.index(axis) if axis in axisOrder else 0x10000 for axis in orderedAxes), # Next, by known axes
					tuple(orderedAxes), # Next, by all axes
					tuple(sign(loc[axis]) for axis in orderedAxes), # Next, by signs of axis values
					tuple(abs(loc[axis]) for axis in orderedAxes), # Next, by absolute value of axis values
				)
			return key

		ret = getKey(axisPoints, axisOrder)
		ret.axisPoints = axisPoints
		return ret

	@staticmethod
	def lowerBound(value, lst):
		if any(v < value for v in lst):
			return max(v for v in lst if v < value)
		else:
			return value
	@staticmethod
	def upperBound(value, lst):
		if any(v > value for v in lst):
			return min(v for v in lst if v > value)
		else:
			return value

	def _computeMasterSupports(self, axisPoints):
		supports = []
		deltaWeights = []
		locations = self.sortedLocations
		for i,loc in enumerate(locations):
			box = {}

			# Account for axisPoints first
			for axis,values in axisPoints.items():
				if not axis in loc:
					continue
				locV = loc[axis]
				box[axis] = (self.lowerBound(locV, values), locV, self.upperBound(locV, values))

			locAxes = set(loc.keys())
			# Walk over previous masters now
			for j,m in enumerate(locations[:i]):
				# Master with extra axes do not participte
				if not set(m.keys()).issubset(locAxes):
					continue
				# If it's NOT in the current box, it does not participate
				relevant = True
				for axis, (lower,_,upper) in box.items():
					if axis in m and not (lower < m[axis] < upper):
						relevant = False
						break
				if not relevant:
					continue
				# Split the box for new master
				for axis,val in m.items():
					assert axis in box
					lower,locV,upper = box[axis]
					if val < locV:
						lower = val
					elif locV < val:
						upper = val
					box[axis] = (lower,locV,upper)
			supports.append(box)

			deltaWeight = []
			# Walk over previous masters now, populate deltaWeight
			for j,m in enumerate(locations[:i]):
				scalar = 1.
				support = supports[j]
				for axis,v in m.items():
					lower, peak, upper = support[axis]
					if axis not in loc:
						scalar = 0.
						break
					v = loc[axis]
					if v == peak:
						continue
					if v <= lower or upper <= v:
						scalar = 0.
						break;
					if v < peak:
						scalar *= (v - peak) / (lower - peak)
					else: # v > peak
						scalar *= (v - peak) / (upper - peak)
				deltaWeight.append(-scalar)
			deltaWeight.append(+1.)
			deltaWeights.append(deltaWeight)

		mapping = self.reverseMapping
		self.supports = [supports[mapped] for mapped in mapping]
		mapping = self.mapping
		self.deltaWeights = {mapping[i]:{mapping[i]:off for i,off in enumerate(deltaWeight) if off != 0.}
				     for i,deltaWeight in enumerate(deltaWeights)}

	def getDeltas(self, masterValues):
		count = len(self.mapping)
		assert len(masterValues) == count
		out = list(masterValues)
		for i in range(count):
			j = self.mapping[i]
			weights = self.deltaWeights[j]
			items = [out[idx] * weight for idx,weight in weights.items()]
			delta = reduce(operator.iadd, items)
			out[j] = delta
		return out

#
# .designspace routines
#

def _xmlParseLocation(et):
	loc = {}
	for dim in et.find('location'):
		assert dim.tag == 'dimension'
		name = dim.attrib['name']
		value = float(dim.attrib['xvalue'])
		assert name not in loc
		loc[name] = value
	return loc

def _designspace_load(et):
	base_idx = None
	masters = []
	ds = et.getroot()
	for master in ds.find('sources'):
		name = master.attrib['name']
		filename = master.attrib['filename']
		isBase = master.find('info')
		if isBase is not None:
			assert base_idx is None
			base_idx = len(masters)
		loc = _xmlParseLocation(master)
		masters.append((filename, loc, name))

	instances = []
	for instance in ds.find('instances'):
		name = master.attrib['name']
		family = instance.attrib['familyname']
		style = instance.attrib['stylename']
		filename = instance.attrib['filename']
		loc = _xmlParseLocation(instance)
		instances.append((filename, loc, name, family, style))

	return masters, instances, base_idx

def designspace_load(filename):
	return _designspace_load(ET.parse(filename))

def designspace_loads(string):
	return _designspace_load(ET.fromstring(string))


#
# Creation routines
#

# TODO: Move to name table proper; also, is mac_roman ok for ASCII names?
def _AddName(font, name):
	"""(font, "Bold") --> NameRecord"""
	nameTable = font.get("name")
	namerec = NameRecord()
	namerec.nameID = 1 + max([n.nameID for n in nameTable.names] + [256])
	namerec.string = name.encode("mac_roman")
	namerec.platformID, namerec.platEncID, namerec.langID = (1, 0, 0)
	nameTable.names.append(namerec)
	return namerec

# Move to fvar table proper?
def _add_fvar(font, axes, instances):
	assert "fvar" not in font
	font['fvar'] = fvar = table__f_v_a_r()

	for tag in sorted(axes.keys()):
		axis = Axis()
		axis.axisTag = tag
		name, axis.minValue, axis.defaultValue, axis.maxValue = axes[tag]
		axis.nameID = _AddName(font, name).nameID
		fvar.axes.append(axis)

	for name, coordinates in instances:
		inst = NamedInstance()
		inst.nameID = _AddName(font, name).nameID
		inst.coordinates = coordinates
		fvar.instances.append(inst)

# TODO Move to glyf or gvar table proper
def _GetCoordinates(font, glyphName):
	"""font, glyphName --> glyph coordinates as expected by "gvar" table

	The result includes four "phantom points" for the glyph metrics,
	as mandated by the "gvar" spec.
	"""
	glyf = font["glyf"]
	if glyphName not in glyf.glyphs: return None
	glyph = glyf[glyphName]
	if glyph.isComposite():
		coord = GlyphCoordinates([c.getComponentInfo()[1][-2:] for c in glyph.components])
		control = [c.glyphName for c in glyph.components]
	else:
		allData = glyph.getCoordinates(glyf)
		coord = allData[0]
		control = allData[1:]

	# Add phantom points for (left, right, top, bottom) positions.
	horizontalAdvanceWidth, leftSideBearing = font["hmtx"].metrics[glyphName]
	if not hasattr(glyph, 'xMin'):
		glyph.recalcBounds(glyf)
	leftSideX = glyph.xMin - leftSideBearing
	rightSideX = leftSideX + horizontalAdvanceWidth
	# XXX these are incorrect.  Load vmtx and fix.
	topSideY = glyph.yMax
	bottomSideY = -glyph.yMin
	coord.extend([(leftSideX, 0),
	              (rightSideX, 0),
	              (0, topSideY),
	              (0, bottomSideY)])

	return coord, control

def _sub(al, bl):
	return [(ax-bx,ay-by) for (ax,ay),(bx,by) in zip(al,bl)]

def _add_gvar(font, axes, master_ttfs, master_locs, base_idx):

	# Make copies for modification
	master_ttfs = master_ttfs[:]
	master_locs = [l.copy() for l in master_locs]

	axis_tags = axes.keys()

	# Normalize master_locs
	# https://github.com/behdad/fonttools/issues/313
	for tag,(name,lower,default,upper) in axes.items():
		for l in master_locs:
			v = l[tag]
			if v == default:
				v = 0
			elif v < default:
				v = (v - default) / (default - lower)
			else:
				v = (v - default) / (upper - default)
			l[tag] = v
	# Locations are normalized now

	print("Normalized master positions:")
	from pprint import pprint
	pprint(master_locs)

	print("Generating gvar")
	assert "gvar" not in font
	gvar = font["gvar"] = table__g_v_a_r()
	gvar.version = 1
	gvar.reserved = 0
	gvar.variations = {}

	# Assume single-model for now.
	model = VariationModel(master_locs)

	for glyph in font.getGlyphOrder():

		allData = [_GetCoordinates(m, glyph) for m in master_ttfs]
		allCoords = [d[0] for d in allData]
		allControls = [d[1] for d in allData]
		control = allControls[0]
		if (any(c != control for c in allControls)):
			warnings.warn("glyph %s has incompatible masters; skipping" % glyph)
			continue
		del allControls

		gvar.variations[glyph] = []

		deltas = model.getDeltas(allCoords)
		supports = model.supports
		assert len(deltas) == len(supports)
		for i,(delta,support) in enumerate(zip(deltas, supports)):
			if i == base_idx:
				continue
			var = GlyphVariation(support, delta)
			gvar.variations[glyph].append(var)

def main(args=None):

	import sys
	if args is None:
		args = sys.argv[1:]

	(designspace_filename,) = args
	finder = lambda s: s.replace('master_ufo', 'master_ttf_interpolatable').replace('.ufo', '.ttf')
	axisMap = None # dict mapping axis id to (axis tag, axis name)
	outfile = os.path.splitext(designspace_filename)[0] + '-GX.ttf'

	masters, instances, base_idx = designspace_load(designspace_filename)

	print("Masters:")
	pprint(masters)
	print("Instances:")
	pprint(instances)
	print("Index of base master:", base_idx)

	print("Building GX")
	print("Loading TTF masters")
	basedir = os.path.dirname(designspace_filename)
	master_ttfs = [finder(os.path.join(basedir, m[0])) for m in masters]
	master_fonts = [TTFont(ttf_path) for ttf_path in master_ttfs]

	standard_axis_map = {
		'weight':  ('wght', 'Weight'),
		'width':   ('wdth', 'Width'),
		'slant':   ('slnt', 'Slant'),
		'optical': ('opsz', 'Optical Size'),
	}

	axis_map = standard_axis_map
	if axisMap:
		axis_map = axis_map.copy()
		axis_map.update(axisMap)

	# TODO: For weight & width, use OS/2 values and setup 'avar' mapping.

	# Set up master locations
	master_locs = []
	instance_locs = []
	out = []
	for loc in [m[1] for m in masters+instances]:
		# Apply modifications for default axes; and apply tags
		l = {}
		for axis,value in loc.items():
			tag,name = axis_map[axis]
			l[tag] = value
		out.append(l)
	master_locs = out[:len(masters)]
	instance_locs = out[len(masters):]

	axis_tags = set(master_locs[0].keys())
	assert all(axis_tags == set(m.keys()) for m in master_locs)
	print("Axis tags:", axis_tags)
	print("Master positions:")
	pprint(master_locs)

	# Set up axes
	axes = {}
	axis_names = {}
	for tag,name in axis_map.values():
		if tag not in axis_tags: continue
		axis_names[tag] = name
	for tag in axis_tags:
		default = master_locs[base_idx][tag]
		lower = min(m[tag] for m in master_locs)
		upper = max(m[tag] for m in master_locs)
		name = axis_names[tag]
		axes[tag] = (name, lower, default, upper)
	print("Axes:")
	pprint(axes)

	# Set up named instances
	instance_list = []
	for loc,instance in zip(instance_locs,instances):
		style = instance[4]
		instance_list.append((style, loc))
	# TODO append masters as named-instances as well; needs .designspace change.

	gx = TTFont(master_ttfs[base_idx])

	_add_fvar(gx, axes, instance_list)

	print("Setting up glyph variations")
	_add_gvar(gx, axes, master_fonts, master_locs, base_idx)

	print("Saving GX font", outfile)
	gx.save(outfile)


if __name__ == "__main__":
	import sys
	if len(sys.argv) > 1:
		main()
		sys.exit(0)
	import doctest, sys
	sys.exit(doctest.testmod().failed)
