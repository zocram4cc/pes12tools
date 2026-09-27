import struct
from struct import pack, unpack
import zlib

class InvalidModel(Exception):
	pass

class FatalParserWarning(Exception):
	pass

class ExportError(Exception):
	pass

class ParserSettings:
	def __init__(self):
		self.ignoreParserWarnings = True
		self.strictParsing = False

class MeshAnnotation:
	MESH_NAME = 128
	EXTENSION_HEADER = 129

class ModelFile:
	class VertexDatumType:
		position = 2 # tripleFloat32
		normal = 3 # tripleFloat32
		color = 4 # quadFloat8
		uv0 = 7 # doubleFloat32
		uv1 = 8 # doubleFloat32
		uv2 = 9 # doubleFloat32
		uv3 = 10 # doubleFloat32
		tangent = 15 # tripleFloat32
		bitangent = 16 # tripleFloat32
		boneWeights = 17 # double / triple / quadFloat32
		boneIndices = 18 # quadInt8
	
	class VertexDatumFormat:
		uint16 = 1
		uint32 = 2
		float32 = 3
		doubleFloat32 = 4
		tripleFloat32 = 5
		quadFloat32 = 6
		float32Matrix34 = 7
		quadInt8 = 8
		quadFloat8 = 9
	
	class Vector2:
		def __init__(self):
			self.u = None
			self.v = None
		
		def __init__(self, u, v):
			self.u = u
			self.v = v
	
	class Vector3:
		def __init__(self):
			self.x = None
			self.y = None
			self.z = None
		
		def __init__(self, x, y, z):
			self.x = x
			self.y = y
			self.z = z
	
	class BoundingBox:
		def __init__(self):
			self.min = None
			self.max = None
		
		def __init__(self, min, max):
			self.min = min
			self.max = max
	
	class Bone:
		def __init__(self):
			self.name = None
			self.matrix = None
		
		def __init__(self, name, matrix):
			self.name = name
			self.matrix = matrix
	
	class BoneGroup:
		def __init__(self):
			self.bones = []
	
	class VertexFields:
		def __init__(self):
			self.hasNormal = False
			self.hasTangent = False
			self.hasBitangent = False
			self.hasColor = False
			self.hasBoneMapping = False
			self.uvCount = 0
			self.uvEqualities = {}
	
	class Vertex:
		def __init__(self):
			self.position = None
			self.normal = None
			self.tangent = None
			self.bitangent = None
			self.color = None
			self.boneMapping = None
			self.uv = []
	
	class VertexEncoding:
		def __init__(self):
			self.vertex = None
			self.position = None
			self.normal = None
			self.tangent = None
			self.bitangent = None
			self.color = None
			self.boneIndices = None
			self.boneWeights = None
			self.uv = []
	
	class Face:
		def __init__(self, v1, v2, v3):
			self.vertices = [v1, v2, v3]
	
	class Mesh:
		def __init__(self):
			self.vertices = None
			self.faces = None
			self.boneGroup = None
			self.material = None
			self.vertexFields = None
			self.boundingBox = None
			# extension fields
			self.name = None
			self.extensionHeaders = set()
			self.vertexEncodings = None
	
	def __init__(self):
		self.bones = []
		self.materials = []
		self.boundingBox = None
		self.meshes = []
		# extension fields
		self.extensionHeaders = set()
	
	def precomputeVertexEncoding(self):
		for mesh in self.meshes:
			encodeVertices(mesh)
	
	def freeVertexEncoding(self):
		for mesh in self.meshes:
			mesh.vertexEncoding = None
	
	

def readModelBuffer(modelBuffer, parserSettings):
	warnings = []
	
	def parserWarning(message):
		if parserSettings.ignoreParserWarnings:
			warnings.append(message)
		else:
			raise FatalParserWarning(message)
	
	class BufferStream:
		def __init__(self, buffer, offset):
			self.buffer = buffer
			self.offset = offset
			self.cursor = 0
		
		def addressOf(self, offset):
			return self.offset + offset
		
		def cursorAddress(self):
			return self.addressOf(self.cursor)
		
		def streamFrom(self, offset):
			return BufferStream(self.buffer, self.offset + offset)
		
		def bufferSlice(self, offset, length):
			return memoryview(self.buffer)[self.offset + offset : self.offset + offset + length]
		
		def read(self, size):
			if len(self.buffer) < self.offset + self.cursor + size:
				raise InvalidModel("Unexpected end of record")
			output = self.buffer[self.offset + self.cursor : self.offset + self.cursor + size]
			self.cursor += size
			return output
		
		def readString(self):
			string = []
			while True:
				try:
					character = self.read(1)
				except:
					raise InvalidModel("Unexpected end of string")
				if character[0] == 0:
					break
				string.append(character[0])
			return str(bytes(string), 'utf-8')
	
	class RecordArray:
		def __init__(self, header, recordSize, labelledRecords):
			self.header = header
			self.recordSize = recordSize
			self.labelledRecords = labelledRecords
		
		def records(self):
			return [record for (address, record) in self.labelledRecords]
		
		def items(self):
			return self.labelledRecords
		
		def __len__(self):
			return len(self.labelledRecords)
		
		@staticmethod
		def parse(stream, expectedRecordSize = None, expectedHeaderSize = None):
			if expectedRecordSize is not None and not isinstance(expectedRecordSize, list):
				expectedRecordSize = [expectedRecordSize]
			if expectedHeaderSize is not None and not isinstance(expectedHeaderSize, list):
				expectedHeaderSize = [expectedHeaderSize]
			
			toc = stream.read(12)
			(offset, recordCount, recordSize) = unpack('< 3I', toc)
			
			headerSize = offset - len(toc)
			if expectedHeaderSize is not None and headerSize not in expectedHeaderSize:
				raise InvalidModel("Unexpected record array header size")
			if expectedRecordSize is not None and recordSize not in expectedRecordSize:
				raise InvalidModel("Unexpected record array record size")
			
			header = stream.read(headerSize)
			labelledRecords = []
			for i in range(recordCount):
				address = stream.cursorAddress()
				record = stream.read(recordSize)
				labelledRecords.append((address, record))
			return RecordArray(header, recordSize, labelledRecords)
	
	class StructArray(RecordArray):
		def __init__(self, stream, recordArray):
			super().__init__(recordArray.header, recordArray.recordSize, recordArray.labelledRecords)
			self.stream = stream
		
		def address(self):
			return self.addressOf(0)
		
		def addressOf(self, offset):
			return self.stream.addressOf(offset)
		
		def streamFrom(self, offset):
			return self.stream.streamFrom(offset)
		
		@staticmethod
		def parse(stream, expectedRecordSize, expectedHeaderSize = 0):
			recordArray = RecordArray.parse(stream, expectedRecordSize, expectedHeaderSize)
			return StructArray(stream, recordArray)
	
	class MeshGeometry:
		def __init__(self, vertexFields, vertices, vertexEncodings, faces, boundingBox):
			self.vertexFields = vertexFields
			self.vertices = vertices
			self.vertexEncodings = vertexEncodings
			self.faces = faces
			self.boundingBox = boundingBox
	
	def zlibExtract(buffer):
		if len(buffer) < 16:
			return buffer
		(magic, ) = unpack('< 4x 4s 8x', buffer[0:16])
		if magic != b'ESYS':
			return buffer
		return zlib.decompress(buffer[16:])
	
	def parseSections(buffer):
		stream = BufferStream(buffer, 0)
		
		magicBuffer = stream.read(8)
		(magic, ) = unpack('< 5s 3x', magicBuffer)
		if magic != b'MODEL':
			raise InvalidModel("Unexpected magic")
		
		header = stream.read(16)
		(tocOffset, unknown1, majorVersion, unknown2, flags) = unpack('< I HH II', header)
		
		if unknown1 != 0:
			parserWarning("Header unknown1 != 0")
		if majorVersion < 16 or majorVersion > 19:
			print("WARNING: Unsupposed .model format version %s, let's hope this works" % majorVersion)
		if unknown2 != 9:
			parserWarning("Header unknown2 != 0")
		if flags != 0:
			parserWarning("Header flags != 0")
		
		sections = []
		sectionArray = StructArray.parse(stream.streamFrom(tocOffset + len(magicBuffer)), 4)
		for record in sectionArray.records():
			(offset, ) = unpack('< I', record)
			sectionStream = sectionArray.streamFrom(offset)
			sections.append(sectionStream)
		
		return sections
	
	def parseBoneNames(sections):
		boneNames = []
		boneNameArray = StructArray.parse(sections[5], 4)
		for record in boneNameArray.records():
			(offset, ) = unpack('< I', record)
			name = boneNameArray.streamFrom(offset).readString()
			boneNames.append(name)
		return boneNames
	
	def parseBoneGroups(sections):
		boneNames = parseBoneNames(sections)
		
		boneMatrices = []
		boneIndexGroups = {}
		
		boneDataArray = StructArray.parse(sections[0], 4)
		for boneDataRecord in boneDataArray.records():
			(dataOffset, ) = unpack('< I', boneDataRecord)
			dataArray = StructArray.parse(boneDataArray.streamFrom(dataOffset), 12, 4)
			(dataEnum, ) = unpack('< I', dataArray.header)
			if dataEnum != 2:
				parserWarning("Section 0 array header != 2")
			
			#
			# Section 0 structure:
			#
			# The first section 0 entry in each model contains the transformation
			# matrix for each bone. This data structure contains one dataArray record
			# per bone, with entryType=7 and entryCount=1, in matching order with
			# bone names. No known external pointers to the transformation section 0
			# entry exist.
			#
			# Each model also contains one or more bone groups. Each bone group
			# is represented by a section 0 entry, containing a single dataArray
			# record, with entryType=1 and entryCount equal to the number of
			# bones in the group. Mesh definitions contain a pointer to a section 0
			# entry containing a bone group.
			#
			# The transformation matrices are always observed to be in the first
			# section 0 record, with bone groups in the remaining records; but it's
			# unclear whether this is a requirement. So we make the distinction based
			# on the entryType.
			#
			
			for dataRecord in dataArray.records():
				(entryOffset, entryType, entryCount) = unpack('< 3I', dataRecord)
				
				if entryType == 7:
					# This dataRecord describes the bone matrix for one bone
					if entryCount != 1:
						raise InvalidModel("Bone matrix record with count != 1")
					
					matrixRecord = dataArray.streamFrom(entryOffset).read(48)
					matrix = unpack('< 12f', matrixRecord)
					boneMatrices.append(matrix)
				
				elif entryType == 1:
					# This dataRecord describes one bone group
					boneIds = []
					boneGroupStream = dataArray.streamFrom(entryOffset)
					for i in range(entryCount):
						(boneId, ) = unpack('< H', boneGroupStream.read(2))
						boneIds.append(boneId)
					boneIndexGroups[dataArray.address()] = boneIds
		
		if len(boneNames) != len(boneMatrices):
			raise InvalidModel("Incomplete bone definitions")
		
		bones = []
		for (name, matrix) in zip(boneNames, boneMatrices):
			bones.append(ModelFile.Bone(name, matrix))
		
		boneGroups = {}
		for (address, boneIndexGroup) in boneIndexGroups.items():
			boneGroup = ModelFile.BoneGroup()
			for boneIndex in boneIndexGroup:
				if boneIndex >= len(bones):
					raise InvalidModel("Invalid bone referenced in bone group")
				boneGroup.bones.append(bones[boneIndex])
			boneGroups[address] = boneGroup
		
		return (bones, boneGroups)
	
	def parseMaterials(sections):
		materials = {}
		materialArray = StructArray.parse(sections[6], 4)
		for (address, record) in materialArray.items():
			(offset, ) = unpack('< I', record)
			name = materialArray.streamFrom(offset).readString()
			materials[address] = name
		return materials
	
	def parseVertices(section, vertexFieldArray):
		vertexCount = None
		
		vertexFields = ModelFile.VertexFields()
		fieldsSeen = []
		hasUv0 = False
		hasUv1 = False
		hasUv2 = False
		hasUv3 = False
		uvOffsets = {}
		
		positionStream = None
		normalStream = None
		tangentStream = None
		bitangentStream = None
		colorStream = None
		boneIndicesStream = None
		boneWeightsStream = None
		uv0Stream = None
		uv1Stream = None
		uv2Stream = None
		uv3Stream = None
		
		def datumStream(format, offset, count):
			size = struct.calcsize(format)
			buffer = section.bufferSlice(offset, size * count)
			unpackStream = struct.iter_unpack(format, buffer)
			for i in range(count):
				yield next(unpackStream), buffer[i * size : (i + 1) * size]
		
		(unknown3, unknown4) = unpack('< 2I', vertexFieldArray.header)
		if unknown3 != 0:
			#
			# Set to nonzero values for models that have content in section 8, and an
			# accompanying .cloth file. Presumably this is a model with cloth physics.
			# The plugin does not support this.
			#
			parserWarning("Cloth physics detected, which is not supported")
		if unknown4 != 0:
			parserWarning("Geometry unknown4 != 0")
		
		for vertexFieldRecord in vertexFieldArray.records():
			(fieldOffset, datumType, datumFormat, fieldVertexCount, unknown5) = unpack('< 5I', vertexFieldRecord)
			
			if unknown5 != 0:
				parserWarning("Geometry unknown5 != 0")
			
			if datumType in fieldsSeen:
				if parserSettings.strictParsing:
					raise InvalidModel("Duplicate vertex field %s found in vertex format definition" % datumType)
				else:
					continue
			fieldsSeen.append(datumType)
			
			if vertexCount is None:
				vertexCount = fieldVertexCount
			elif vertexCount != fieldVertexCount:
				if parserSettings.strictParsing:
					raise InvalidModel("Vertex fields with conflicting vertex count found in vertex format definition")
				else:
					continue
			
			if datumType == ModelFile.VertexDatumType.position:
				if datumFormat == ModelFile.VertexDatumFormat.tripleFloat32:
					positionStream = datumStream('< 3f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex position data" % datumFormat)
			elif datumType == ModelFile.VertexDatumType.normal:
				if datumFormat == ModelFile.VertexDatumFormat.tripleFloat32:
					normalStream = datumStream('< 3f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex normal data" % datumFormat)
				vertexFields.hasNormal = True
			elif datumType == ModelFile.VertexDatumType.tangent:
				if datumFormat == ModelFile.VertexDatumFormat.tripleFloat32:
					tangentStream = datumStream('< 3f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex tangent data" % datumFormat)
				vertexFields.hasTangent = True
			elif datumType == ModelFile.VertexDatumType.bitangent:
				if datumFormat == ModelFile.VertexDatumFormat.tripleFloat32:
					bitangentStream = datumStream('< 3f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex bitangent data" % datumFormat)
				vertexFields.hasBitangent = True
			elif datumType == ModelFile.VertexDatumType.color:
				if datumFormat == ModelFile.VertexDatumFormat.quadFloat8:
					colorStream = datumStream('< 4B', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex color data" % datumFormat)
				vertexFields.hasColor = True
			elif datumType == ModelFile.VertexDatumType.boneIndices:
				if datumFormat == ModelFile.VertexDatumFormat.quadInt8:
					boneIndicesStream = datumStream('< 4B', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex bone indices data" % datumFormat)
				vertexFields.hasBoneMapping = True
			elif datumType == ModelFile.VertexDatumType.boneWeights:
				if datumFormat == ModelFile.VertexDatumFormat.quadFloat32:
					boneWeightsStream = datumStream('< 4f', fieldOffset, vertexCount)
				elif datumFormat == ModelFile.VertexDatumFormat.tripleFloat32:
					boneWeightsStream = datumStream('< 3f', fieldOffset, vertexCount)
				elif datumFormat == ModelFile.VertexDatumFormat.doubleFloat32:
					boneWeightsStream = datumStream('< 2f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex bone weights data" % datumFormat)
			elif datumType == ModelFile.VertexDatumType.uv0:
				if datumFormat == ModelFile.VertexDatumFormat.doubleFloat32:
					uv0Stream = datumStream('< 2f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex uv0 data" % datumFormat)
				hasUv0 = True
				uvOffsets[0] = fieldOffset
				vertexFields.uvCount += 1
			elif datumType == ModelFile.VertexDatumType.uv1:
				if datumFormat == ModelFile.VertexDatumFormat.doubleFloat32:
					uv1Stream = datumStream('< 2f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex uv1 data" % datumFormat)
				hasUv1 = True
				uvOffsets[1] = fieldOffset
				vertexFields.uvCount += 1
			elif datumType == ModelFile.VertexDatumType.uv2:
				if datumFormat == ModelFile.VertexDatumFormat.doubleFloat32:
					uv2Stream = datumStream('< 2f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex uv2 data" % datumFormat)
				hasUv2 = True
				uvOffsets[2] = fieldOffset
				vertexFields.uvCount += 1
			elif datumType == ModelFile.VertexDatumType.uv3:
				if datumFormat == ModelFile.VertexDatumFormat.doubleFloat32:
					uv3Stream = datumStream('< 2f', fieldOffset, vertexCount)
				else:
					raise InvalidModel("Unexpected format %d for vertex uv3 data" % datumFormat)
				hasUv3 = True
				uvOffsets[3] = fieldOffset
				vertexFields.uvCount += 1
		
		#
		# TODO: Some stock models have this. Can this just be compactified?
		#
		
		if hasUv3 and not hasUv2:
			parserWarning("Non-monotonic UV map in vertex format definition: has uv3 but not uv2")
		if hasUv2 and not hasUv1:
			parserWarning("Non-monotonic UV map in vertex format definition: has uv2 but not uv1")
		if hasUv1 and not hasUv0:
			parserWarning("Non-monotonic UV map in vertex format definition: has uv1 but not uv0")
		
		#for i in range(vertexFields.uvCount):
		#	vertexFields.uvEqualities[i] = []
		#	for j in range(vertexFields.uvCount):
		#		if i != j and uvOffsets[i] == uvOffsets[j]:
		#			vertexFields.uvEqualities[i].append(j)
		
		vertices = []
		vertexEncodings = []
		for i in range(vertexCount):
			vertex = ModelFile.Vertex()
			vertexEncoding = ModelFile.VertexEncoding()
			vertexEncoding.vertex = vertex
			
			if positionStream is not None:
				(position, vertexEncoding.position) = next(positionStream)
				vertex.position = ModelFile.Vector3(*position)
			if normalStream is not None:
				(normal, vertexEncoding.normal) = next(normalStream)
				vertex.normal = ModelFile.Vector3(*normal)
			if tangentStream is not None:
				(tangent, vertexEncoding.tangent) = next(tangentStream)
				vertex.tangent = ModelFile.Vector3(*tangent)
			if bitangentStream is not None:
				(bitangent, vertexEncoding.bitangent) = next(bitangentStream)
				vertex.bitangent = ModelFile.Vector3(*bitangent)
			if colorStream is not None:
				(color, vertexEncoding.color) = next(colorStream)
				vertex.color = tuple(c / 255.0 for c in color)
			if boneIndicesStream is not None:
				(boneIndices, vertexEncoding.boneIndices) = next(boneIndicesStream)
				if boneWeightsStream is not None:
					(boneWeights, vertexEncoding.boneWeights) = next(boneWeightsStream)
					vertex.boneMapping = {}
					for (index, weight) in zip(boneIndices, boneWeights):
						if index not in vertex.boneMapping and weight > 0.0:
							vertex.boneMapping[index] = weight
				else:
					vertex.boneMapping = { boneIndices[0]: 1.0 }
			if uv0Stream is not None:
				(uv, uvEncoding) = next(uv0Stream)
				vertex.uv.append(ModelFile.Vector2(*uv))
				vertexEncoding.uv.append(uvEncoding)
			if uv1Stream is not None:
				(uv, uvEncoding) = next(uv1Stream)
				vertex.uv.append(ModelFile.Vector2(*uv))
				vertexEncoding.uv.append(uvEncoding)
			if uv2Stream is not None:
				(uv, uvEncoding) = next(uv2Stream)
				vertex.uv.append(ModelFile.Vector2(*uv))
				vertexEncoding.uv.append(uvEncoding)
			if uv3Stream is not None:
				(uv, uvEncoding) = next(uv3Stream)
				vertex.uv.append(ModelFile.Vector2(*uv))
				vertexEncoding.uv.append(uvEncoding)
			
			vertices.append(vertex)
			vertexEncodings.append(vertexEncoding)
		
		return (vertexFields, vertices, vertexEncodings)
	
	def parseFacesWithLooseVertexRecovery(faceVertexStream, faceVertexCount, vertices):
		#
		# This mesh was exported by the old plugin while it had loose vertices. Those loose vertices are not part of
		# the vertices table, but they do occupy space in the vertex ID space. As a consequence, the mesh is corrupt,
		# as the vertex IDs used in the face list refer to vertex IDs that may be $numberOfLooseVertices too high.
		#
		# We can compensate for this corruption by interpreting each vertex ID in the face list as referring to the vertex
		# (apparent vertex ID - number of unused vertex IDs that referred to loose vertices, smaller than apparent vertex ID).
		#
		verticesSeen = set()
		faceVertexIDs = []
		for i in range(faceVertexCount // 3):
			(index1, index2, index3) = unpack('< 3H', faceVertexStream.read(6))
			verticesSeen.add(index1)
			verticesSeen.add(index2)
			verticesSeen.add(index3)
		
		if len(verticesSeen) != len(vertices):
			return None
		
		effectiveVertexIDs = {}
		for i in range(max(verticesSeen) + 1):
			if i in verticesSeen:
				effectiveVertexIDs[i] = len(effectiveVertexIDs)
		
		faceVertexStream = faceVertexStream.streamFrom(0)
		faces = []
		for i in range(faceVertexCount // 3):
			(index1, index2, index3) = unpack('< 3H', faceVertexStream.read(6))
			if index1 == index2 or index1 == index3 or index2 == index3:
				continue
			faces.append(ModelFile.Face(
				vertices[effectiveVertexIDs[index1]],
				vertices[effectiveVertexIDs[index2]],
				vertices[effectiveVertexIDs[index3]],
			))
		return faces
	
	def parseFaces(section, faceDescriptorRecord, vertices):
		(facesStartOffset, unknown6, dataFormat, faceVertexCount, lodLevels, lodOffset) = unpack('< 6I', faceDescriptorRecord)
		
		if dataFormat != ModelFile.VertexDatumFormat.uint16:
			parserWarning("Face vertex format != uint16")
		if unknown6 != 1:
			parserWarning("Geometry unknown6 != 1")
		
		if lodLevels != 0:
			#
			# Each LOD includes a different set of faces, in order of decreasing quality.
			# We only parse the first one and ignore the rest.
			#
			firstLodRecord = section.streamFrom(lodOffset).read(8)
			(startFaceVertex, endFaceVertex) = unpack('< 2I', firstLodRecord)
		else:
			startFaceVertex = 0
			endFaceVertex = faceVertexCount
		
		faceVertexStream = section.streamFrom(facesStartOffset + 2 * startFaceVertex)
		faceVertexCount = endFaceVertex - startFaceVertex
		faces = []
		for i in range(faceVertexCount // 3):
			(index1, index2, index3) = unpack('< 3H', faceVertexStream.read(6))
			if index1 == index2 or index1 == index3 or index2 == index3:
				continue
			if index1 >= len(vertices) or index2 >= len(vertices) or index3 >= len(vertices):
				if not parserSettings.strictParsing:
					#
					# Try a second, slower parse using loose vertex recovery
					# If it succeeds, swallow the error
					#
					faces = parseFacesWithLooseVertexRecovery(faceVertexStream.streamFrom(0), faceVertexCount, vertices)
					if faces is not None:
						break
				
				raise InvalidModel("Invalid vertex referenced by face")
			faces.append(ModelFile.Face(vertices[index1], vertices[index2], vertices[index3]))
		
		return faces
	
	def parseMeshGeometries(sections):
		geometries = {}
		geometryArray = StructArray.parse(sections[1], [16, 20])
		for (address, record) in geometryArray.items():
			(unknown1, vertexSetOffset, faceDescriptorOffset, unknown2) = unpack('< 4I', record[0:16])
			if len(record) >= 20:
				(miscOffset, ) = unpack('< I', record[16:20])
			else:
				miscOffset = 0
			
			if unknown1 != 1:
				parserWarning("Geometry unknown1 != 1")
			if unknown2 != 0:
				parserWarning("Geometry unknown2 != 0")
			
			vertexSetArray = StructArray.parse(geometryArray.streamFrom(vertexSetOffset), 4)
			if len(vertexSetArray) != 1:
				raise InvalidModel("Number of geometry vertex sets != 1")
			vertexSetRecord = list(vertexSetArray.records())[0]
			(fieldsOffset, ) = unpack('< I', vertexSetRecord)
			vertexFieldArray = StructArray.parse(geometryArray.streamFrom(fieldsOffset), 20, 8)
			
			(vertexFields, vertices, vertexEncodings) = parseVertices(sections[1], vertexFieldArray)
			
			faceDescriptorArray = StructArray.parse(geometryArray.streamFrom(faceDescriptorOffset), 24)
			if len(faceDescriptorArray) != 1:
				raise InvalidModel("Unexpected number of face header array entries")
			faceDescriptorRecord = list(faceDescriptorArray.records())[0]
			
			faces = parseFaces(sections[1], faceDescriptorRecord, vertices)
			
			miscDataOffsets = []
			if miscOffset != 0:
				miscDataArray = StructArray.parse(geometryArray.streamFrom(miscOffset), 4)
				for miscDataRecord in miscDataArray.records():
					(miscDataOffset, ) = unpack('< I', miscDataRecord)
					miscDataOffsets.append(miscDataOffset)
			
			boundingBox = None
			if len(miscDataOffsets) >= 1:
				boundingBoxRecord = miscDataArray.streamFrom(miscDataOffsets[0]).read(32)
				(minX, minY, minZ, minW, maxX, maxY, maxZ, maxW) = unpack('< 8f', boundingBoxRecord)
				boundingBox = ModelFile.BoundingBox(
					ModelFile.Vector3(minX, minY, minZ),
					ModelFile.Vector3(maxX, maxY, maxZ),
				)
			
			if len(miscDataOffsets) >= 2:
				materialCombinationRecord = miscDataArray.streamFrom(miscDataOffsets[1]).read(16)
				bits = unpack('< 4I', materialCombinationRecord)
				
				#
				# If nonzero, each bit (other than bit 0, which is always 0?) refers to
				# an indexed element of section 9, which contains material names. This field
				# is a bitmask. The other three unknowns in this section may contain more bits.
				#
				# A bitmask suggests that this mesh uses a combination of multiple materials.
				# Overlaid? Unclear.
				#
				# The plugin does not support this.
				#
				
				if bits[0] != 1 or bits[1] != 0 or bits[2] != 0 or bits[3] != 0:
					parserWarning("Material combination detected, which is not supported")
			
			if len(miscDataOffsets) >= 3:
				miscUnknownRecord = miscDataArray.streamFrom(miscDataOffsets[2]).read(4)
				(possibleRenderingOrder, ) = unpack('< I', miscUnknownRecord)
				
				#
				# Integer, meaning unclear.
				# This value is nonzero only in stadium models, and contains numbers from 1 to some N
				# for the different parts of the stadium, even across different .model files,
				# mostly but not entirely without gaps.
				#
				# Could this be a rendering order field?
				#
				# TODO investigate
				#
			
			geometries[address] = MeshGeometry(vertexFields, vertices, vertexEncodings, faces, boundingBox)
		
		return geometries
	
	def parseAnnotationStrings(sections):
		annotationStrings = {}
		annotationStringArray = StructArray.parse(sections[2], 8)
		for (address, record) in annotationStringArray.items():
			(offset, unknown) = unpack('< II', record)
			annotationString = annotationStringArray.streamFrom(offset).readString()
			annotationStrings[address] = annotationString
		return annotationStrings
	
	def parseMeshes(sections, boneGroups, materials, geometries, annotationStrings):
		usedAnnotationStringAddresses = set()
		meshes = []
		meshArray = StructArray.parse(sections[4], [20, 24], 0)
		for meshRecord in meshArray.records():
			(relativeGeometryAddress, boneGroupOffset, annotationsOffset, relativeMaterialAddress, relativeSection10Address) = unpack('< i I I i i', meshRecord[0:20])
			if len(meshRecord) >= 24:
				(unknownDataOffset, ) = unpack('< I', meshRecord[20:24])
			else:
				unknownDataOffset = 0
			
			if boneGroupOffset == 0:
				boneGroupAddress = None
			else:
				boneGroupArray = StructArray.parse(meshArray.streamFrom(boneGroupOffset), 4)
				if len(boneGroupArray) == 0:
					boneGroupAddress = None
				elif len(boneGroupArray) == 1:
					(relativeBoneGroupOffset, ) = unpack('< i', list(boneGroupArray.records())[0])
					boneGroupAddress = meshArray.addressOf(relativeBoneGroupOffset)
				else:
					raise InvalidModel("Unexpected mesh bone group format")
			
			meshName = None
			extensionHeaders = set()
			if annotationsOffset > 0:
				annotationArray = StructArray.parse(meshArray.streamFrom(annotationsOffset), 16)
				for annotationRecord in annotationArray.records():
					(relativeAnnotationStringOffset, section3Offset, unknown, annotationType) = unpack('< 4i', annotationRecord)
					annotationStringAddress = meshArray.addressOf(relativeAnnotationStringOffset)
					if annotationStringAddress in annotationStrings:
						annotationString = annotationStrings[annotationStringAddress]
						usedAnnotationStringAddresses.add(annotationStringAddress)
					else:
						continue
					
					if annotationType == MeshAnnotation.MESH_NAME:
						meshName = annotationString
					elif annotationType == MeshAnnotation.EXTENSION_HEADER:
						extensionHeaders.add(annotationString)
			
			#
			# metadataOffset points to a structure containing metadata not used by PES;
			# probably data used by the original .model editor and not used in loading the model.
			#
			
			#
			# unknownDataOffset also points to a structure probably only used by the original .model
			# editor, but this structure IS read by PES. Best guess is still that it isn't used
			# in loading the model, but it's less clear.
			#
			
			# if unknownDataOffset != 0:
				# unknownDataArray = StructArray.parse(meshArray.streamFrom(unknownDataOffset), 4)
				
				#
				# each record is 4 bytes long and contains a single offset relative to unknownDataStream.
				# This offset points to a data structure:
				# - uint32 type
				# - uint32 unknown
				# - variant value
				# if type == 1, value is a null-terminated string, aligned at the end to a 4-byte boundary.
				# for any other type, value is a uint32.
				#
				# Example content: [
				#     (1, 0, "TI_Mod_WEInfo_Stadium"),
				#     (1, 0, "PES2010"),
				#     (1, 0, "TI_Util_CN_InfoNode"),
				#     (1, 0, "Common"),
				#     (2, 0, 0),
				#     (2, 0, 0),
				#     (1, 0, "TI_Util_CN_InfoNode_AttrBlock"),
				#     (1, 0, "Stadium"),
				#     (2, 0, 0),
				#     (2, 0, 0),
				#     (2, 0, 0),
				#     (2, 0, 0),
				#     (3, 0, 0x13a9),
				#     (1, 0, "TI_Util_CN_InfoNode_AttrBlock"),
				# ]
				#
				# No known player models have this information, but many stadium, staff, billboards, and props models do.
				# Best guess: metadata for whatever editor tool was used to generate the model.
				#
			
			if relativeSection10Address != 0:
				#
				# Section 10 contains additional geometry of an unknown format used by so-called locator models.
				# The plugin doesn't support this.
				#
				parserWarning("Locator model detected, which is not supported")
			
			geometryAddress = meshArray.addressOf(relativeGeometryAddress)
			materialAddress = meshArray.addressOf(relativeMaterialAddress)
			
			if geometryAddress not in geometries:
				raise InvalidModel("Invalid geometry referenced by mesh")
			geometry = geometries[geometryAddress]
			
			if materialAddress not in materials:
				raise InvalidModel("Invalid material referenced by mesh")
			material = materials[materialAddress]
			
			if boneGroupAddress is None:
				boneGroup = None
			else:
				if boneGroupAddress not in boneGroups:
					raise InvalidModel("Invalid bone group referenced by mesh")
				boneGroup = boneGroups[boneGroupAddress]
			
			mesh = ModelFile.Mesh()
			mesh.vertices = geometry.vertices
			mesh.faces = geometry.faces
			mesh.boneGroup = boneGroup
			mesh.material = material
			mesh.vertexFields = geometry.vertexFields
			mesh.vertexEncodings = geometry.vertexEncodings
			mesh.boundingBox = geometry.boundingBox
			mesh.name = meshName
			mesh.extensionHeaders = extensionHeaders
			meshes.append(mesh)
		
		modelAnnotations = set(value for key, value in annotationStrings.items() if key not in usedAnnotationStringAddresses)
		
		return (meshes, modelAnnotations)
	
	
	buffer = zlibExtract(modelBuffer)
	sections = parseSections(buffer)
	
	(bones, boneGroups) = parseBoneGroups(sections)
	materials = parseMaterials(sections)
	geometries = parseMeshGeometries(sections)
	annotationStrings = parseAnnotationStrings(sections)
	(meshes, modelAnnotations) = parseMeshes(sections, boneGroups, materials, geometries, annotationStrings)
	
	#
	# Sections 2 and 3 contains information unused by PES, likely relevant to the master
	# .model editor but not PES. This information is referenced in section 4. The plugin
	# does not parse either the sections or the references.
	#
	
	#
	# Section 7 contains a bounding box for the whole model, and LOD selection parameters.
	# The plugin doesn't parse either, but generates sensible ones on export.
	#
	
	#
	# Section 8 contains suspected cloth physics information, referenced in section 1.
	# The plugin does not parse the section; references to it cause a warning.
	#
	
	#
	# Section 9 contains suspected material combination information, referenced in section 1.
	# The plugin does not parse the section; references to it cause a warning.
	#
	
	#
	# Section 10 contains suspected locator model information, referenced in section 4.
	# The plugin does not parse the section; references to it cause a warning.
	#
	
	output = ModelFile()
	output.bones = bones
	output.materials = materials
	output.meshes = meshes
	output.extensionHeaders = modelAnnotations
	return (output, warnings)

def readModelStream(stream, parserSettings):
	return readModelBuffer(stream.read(), parserSettings)

def readModelFile(filename, parserSettings):
	with open(filename, 'rb') as stream:
		return readModelStream(open(filename, 'rb'), parserSettings)



def encodeBoneMapping(boneMapping):
	#
	# .model bone mappings support at most 4 bones. If a vertex has more than four bones in its bone mapping,
	# cut it down to the four bones with the most weight; redistribute all weight omitted this way along the
	# remaining four bones, proportional to their original weight.
	#
	
	orderedBones = sorted(boneMapping.items(), key = (lambda pair: (pair[1], pair[0])), reverse = True)
	totalWeight = sum([weight for (boneIndex, weight) in orderedBones])
	selectedBones = orderedBones[0:4]
	selectedWeight = sum([weight for (boneIndex, weight) in selectedBones])
	missingWeight = totalWeight - selectedWeight
	
	if missingWeight > 0:
		effectiveMapping = [(boneIndex, weight + (missingWeight * (weight / selectedWeight))) for (boneIndex, weight) in selectedBones]
	else:
		effectiveMapping = selectedBones
	
	if len(effectiveMapping) < 4:
		effectiveMapping += [(0, 0.0)] * (4 - len(effectiveMapping))
	
	boneIndices = pack('< 4B', *(boneIndex for (boneIndex, weight) in effectiveMapping))
	boneWeights = pack('< 4f', *(weight for (boneIndex, weight) in effectiveMapping))
	return (boneIndices, boneWeights)

def encodeVertices(mesh):
	if mesh.vertexEncodings is not None:
		return
	
	mesh.vertexEncodings = []
	for vertex in mesh.vertices:
		encoding = ModelFile.VertexEncoding()
		encoding.vertex = vertex
		encoding.position = pack('< 3f', vertex.position.x, vertex.position.y, vertex.position.z)
		mesh.vertexEncodings.append(encoding)
	
	if mesh.vertexFields.hasNormal:
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.normal = pack('< 3f', vertex.normal.x, vertex.normal.y, vertex.normal.z)
	
	if mesh.vertexFields.hasTangent:
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.tangent = pack('< 3f', vertex.tangent.x, vertex.tangent.y, vertex.tangent.z)
	
	if mesh.vertexFields.hasBitangent:
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.bitangent = pack('< 3f', vertex.bitangent.x, vertex.bitangent.y, vertex.bitangent.z)
	
	if mesh.vertexFields.hasColor:
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.color = pack('< 4B', *(int(x * 255 + 0.5) for x in vertex.color))
	
	for i in range(mesh.vertexFields.uvCount):
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.uv.append(pack('< 2f', vertex.uv[i].u, vertex.uv[i].v))
	
	if mesh.vertexFields.hasBoneMapping:
		for (vertex, encoding) in zip(mesh.vertices, mesh.vertexEncodings):
			encoding.boneIndices, encoding.boneWeights = encodeBoneMapping(vertex.boneMapping)

def writeModel(model):
	def pad(blob, size):
		if len(blob) % size == 0:
			return blob
		padding = size - (len(blob) % size)
		return blob + bytearray(padding)
	
	def relativizeAddresses(addresses, offset):
		return { key: address + offset for (key, address) in addresses.items() }
	
	class RecordArray:
		def __init__(self, recordSize, header = None):
			self.recordSize = recordSize
			self.header = bytes() if header is None else header
			self.records = []
		
		def recordOffset(self, recordIndex):
			return self.recordSize * recordIndex + len(self.header) + 12
		
		def addRecord(self, record):
			if len(record) != self.recordSize:
				raise ExportError('Unexpected record size')
			offset = self.recordOffset(len(self.records))
			self.records.append(record)
			return offset
		
		def encode(self):
			arrayHeader = pack('< III',
				len(self.header) + 12,
				len(self.records),
				self.recordSize,
			)
			return b''.join([arrayHeader] + [self.header] + self.records)
	
	class StructArray:
		def __init__(self, recordSize, recordCount, header = None):
			self.recordArray = RecordArray(recordSize, header)
			self.expectedRecordCount = recordCount
			self.blobs = []
		
		def nextBlobOffset(self):
			return self.recordArray.recordOffset(self.expectedRecordCount) + sum(len(b) for b in self.blobs)
		
		def addRecord(self, record):
			return self.recordArray.addRecord(record)
		
		def addBlob(self, blob):
			offset = self.nextBlobOffset()
			self.blobs.append(blob)
			return offset
		
		def encode(self):
			if len(self.recordArray.records) != self.expectedRecordCount:
				raise ExportError('Unexpected record count')
			
			return b''.join([self.recordArray.encode()] + self.blobs)
	
	class Sections:
		def __init__(self, sectionCount):
			self.sectionCount = sectionCount
			self.sections = StructArray(4, sectionCount)
			self.sectionOffsets = {}
		
		def nextSectionAddress(self):
			return self.sections.nextBlobOffset()
		
		def addSection(self, sectionNumber, blob):
			if sectionNumber >= self.sectionCount:
				raise ExportError('Unexpected section number')
			if sectionNumber in self.sectionOffsets:
				raise ExportError('Duplicate section')
			
			sectionOffset = self.sections.addBlob(pad(blob, 4))
			self.sectionOffsets[sectionNumber] = sectionOffset
			return sectionOffset
		
		def encode(self):
			if self.sectionCount != len(self.sectionOffsets):
				raise ExportError('Incomplete sections')
			for i in range(self.sectionCount):
				self.sections.addRecord(pack('< I', self.sectionOffsets[i]))
			
			header = pack('< 5s 3x I HH II',
				b'MODEL',
				16, # section array offset
				0,  # unknown
				19, # format version
				9,  # unknown
				0,  # flags
			)
			return header + self.sections.encode()
	
	def storeMetadata(sections, model):
		section7 = StructArray(4, 2)
		
		boundingBoxOffset = section7.addBlob(pack('< 8f',
			model.boundingBox.min.x, model.boundingBox.min.y, model.boundingBox.min.z, 0,
			model.boundingBox.max.x, model.boundingBox.max.y, model.boundingBox.max.z, 0,
		))
		section7.addRecord(pack('< I', boundingBoxOffset))
		
		# Probably LOD switching data
		lodDataOffset = section7.addBlob(pack('< I fff', 0, 0.0625, 4.0, 0.0))
		section7.addRecord(pack('< I', lodDataOffset))
		
		sections.addSection(7, section7.encode())
	
	def storeBones(sections, bones, boneGroups):
		boneNames = StructArray(4, len(model.bones))
		boneMatrices = StructArray(12, len(model.bones), pack('< I', 2))
		boneIndices = {}
		
		for bone in bones:
			boneIndices[bone] = len(boneNames.recordArray.records)
			
			nameBlob = bone.name.encode('utf-8') + b'\0'
			nameOffset = boneNames.addBlob(nameBlob)
			boneNames.addRecord(pack('< I', nameOffset))
			
			matrixBlob = pack('< 12f', *bone.matrix)
			matrixOffset = boneMatrices.addBlob(matrixBlob)
			boneMatrices.addRecord(pack('< 3I', matrixOffset, ModelFile.VertexDatumFormat.float32Matrix34, 1))
		
		section0 = StructArray(4, len(boneGroups) + 1)
		boneMatricesOffset = section0.addBlob(boneMatrices.encode())
		section0.addRecord(pack('< I', boneMatricesOffset))
		
		boneGroupAddresses = {}
		
		for boneGroup in boneGroups:
			boneGroupArray = StructArray(12, 1, pack('< I', 2))
			
			boneGroupBlob = bytearray()
			for bone in boneGroup.bones:
				boneGroupBlob += pack('< H', boneIndices[bone])
			
			boneGroupOffset = boneGroupArray.addBlob(pad(boneGroupBlob, 4))
			boneGroupArray.addRecord(pack('< 3I', boneGroupOffset, ModelFile.VertexDatumFormat.uint16, len(boneGroup.bones)))
			
			boneGroupSection0Offset = section0.addBlob(boneGroupArray.encode())
			section0.addRecord(pack('< I', boneGroupSection0Offset))
			boneGroupAddresses[boneGroup] = boneGroupSection0Offset
		
		section0Offset = sections.addSection(0, section0.encode())
		sections.addSection(5, boneNames.encode())
		return relativizeAddresses(boneGroupAddresses, section0Offset)
	
	def storeMaterials(sections, materials):
		materialSection = StructArray(4, len(materials))
		materialAddresses = {}
		
		for material in materials:
			materialBlob = material.encode('utf-8') + b'\0'
			materialOffset = materialSection.addBlob(materialBlob)
			materialAddresses[material] = materialSection.addRecord(pack('< I', materialOffset))
		
		sectionOffset = sections.addSection(6, materialSection.encode())
		return relativizeAddresses(materialAddresses, sectionOffset)
	
	def storeMeshGeometry(geometrySection, mesh):
		encodeVertices(mesh)
		
		vertexFieldArray = RecordArray(20, pack('< 2I',
			0, # disable cloth physics
			0, # unknown
		))
		def addField(bufferOffset, datumType, datumFormat):
			vertexFieldArray.addRecord(pack('< 5I',
				bufferOffset,
				datumType,
				datumFormat,
				len(mesh.vertices),
				0, # unknown
			))
		
		if True:
			# position is always present
			offset = geometrySection.addBlob(b''.join(v.position for v in mesh.vertexEncodings))
			addField(offset, ModelFile.VertexDatumType.position, ModelFile.VertexDatumFormat.tripleFloat32)
		if mesh.vertexFields.hasNormal:
			offset = geometrySection.addBlob(b''.join(v.normal for v in mesh.vertexEncodings))
			addField(offset, ModelFile.VertexDatumType.normal, ModelFile.VertexDatumFormat.tripleFloat32)
		if mesh.vertexFields.hasTangent:
			offset = geometrySection.addBlob(b''.join(v.tangent for v in mesh.vertexEncodings))
			addField(offset, ModelFile.VertexDatumType.tangent, ModelFile.VertexDatumFormat.tripleFloat32)
		if mesh.vertexFields.hasBitangent:
			offset = geometrySection.addBlob(b''.join(v.bitangent for v in mesh.vertexEncodings))
			addField(offset, ModelFile.VertexDatumType.bitangent, ModelFile.VertexDatumFormat.tripleFloat32)
		if mesh.vertexFields.hasColor:
			offset = geometrySection.addBlob(b''.join(v.color for v in mesh.vertexEncodings))
			addField(offset, ModelFile.VertexDatumType.color, ModelFile.VertexDatumFormat.quadFloat8)
		for i in range(mesh.vertexFields.uvCount):
			offset = geometrySection.addBlob(b''.join(v.uv[i] for v in mesh.vertexEncodings))
			datumType = [ModelFile.VertexDatumType.uv0, ModelFile.VertexDatumType.uv1, ModelFile.VertexDatumType.uv2, ModelFile.VertexDatumType.uv3][i]
			addField(offset, datumType, ModelFile.VertexDatumFormat.doubleFloat32)
		if mesh.vertexFields.hasBoneMapping:
			indexOffset = geometrySection.addBlob(b''.join(v.boneIndices for v in mesh.vertexEncodings))
			addField(indexOffset, ModelFile.VertexDatumType.boneIndices, ModelFile.VertexDatumFormat.quadInt8)
			
			weightOffset = geometrySection.addBlob(b''.join(v.boneWeights for v in mesh.vertexEncodings))
			addField(weightOffset, ModelFile.VertexDatumType.boneWeights, ModelFile.VertexDatumFormat.quadFloat32)
		
		vertexFieldsOffset = geometrySection.addBlob(vertexFieldArray.encode())
		vertexSetArray = RecordArray(4)
		vertexSetArray.addRecord(pack('< I', vertexFieldsOffset))
		vertexSetOffset = geometrySection.addBlob(vertexSetArray.encode())
		
		
		
		vertexIndices = { vertex: index for vertex, index in zip(mesh.vertices, range(len(mesh.vertices))) }
		encodedFaces = [pack('< 3H', *(vertexIndices[v] for v in f.vertices)) for f in mesh.faces]
		facesOffset = geometrySection.addBlob(pad(b''.join(encodedFaces), 4))
		
		faceDescriptorArray = RecordArray(24)
		faceDescriptorArray.addRecord(pack('< 6I',
			facesOffset,
			1, # unknown; datum type?
			ModelFile.VertexDatumFormat.uint16,
			3 * len(mesh.faces),
			0, # lod level count
			0, # lod table offset
		))
		faceDescriptorOffset = geometrySection.addBlob(faceDescriptorArray.encode())
		
		
		
		miscData = StructArray(4, 3)
		
		boundingBoxOffset = miscData.addBlob(pack('< 8f',
			mesh.boundingBox.min.x, mesh.boundingBox.min.y, mesh.boundingBox.min.z, 0,
			mesh.boundingBox.max.x, mesh.boundingBox.max.y, mesh.boundingBox.max.z, 0,
		))
		miscData.addRecord(pack('< I', boundingBoxOffset))
		
		# Material combination flags. 1,0,0,0 means no material combinations are used.
		materialCombinationOffset = miscData.addBlob(pack('< 4I', 1, 0, 0, 0))
		miscData.addRecord(pack('< I', materialCombinationOffset))
		
		# Meaning of this field is guesswork and might be completely wrong
		renderingOrderOffset = miscData.addBlob(pack('< I', 0))
		miscData.addRecord(pack('< I', renderingOrderOffset))
		
		miscDataOffset = geometrySection.addBlob(miscData.encode())
		
		
		
		geometryOffset = geometrySection.addRecord(pack('< 5I',
			1, # unknown
			vertexSetOffset,
			faceDescriptorOffset,
			0, # unknown
			miscDataOffset,
		))
		return geometryOffset
	
	def getMeshAnnotations(mesh):
		annotations = []
		if mesh.name is not None:
			annotations.append((MeshAnnotation.MESH_NAME, mesh.name))
		for header in mesh.extensionHeaders:
			annotations.append((MeshAnnotation.EXTENSION_HEADER, header))
		return annotations
	
	def storeAnnotations(sections, meshAnnotations, modelExtensionHeaders):
		annotationStrings = set(annotationString for annotations in meshAnnotations.values() for (annotationType, annotationString) in annotations)
		annotationSection = StructArray(8, len(annotationStrings) + len(modelExtensionHeaders))
		
		annotationStringRecordOffsets = {}
		for annotationString in annotationStrings:
			annotationBlob = annotationString.encode('utf-8') + b'\0'
			annotationStringOffset = annotationSection.addBlob(annotationBlob)
			annotationRecordOffset = annotationSection.addRecord(pack('< II', annotationStringOffset, 0)) # no clue what the 0 means
			annotationStringRecordOffsets[annotationString] = annotationRecordOffset
		
		for modelExtensionHeader in modelExtensionHeaders:
			headerBlob = modelExtensionHeader.encode('utf-8') + b'\0'
			headerStringOffset = annotationSection.addBlob(headerBlob)
			annotationSection.addRecord(pack('< II', headerStringOffset, 0)) # no clue what the 0 means
		
		sectionOffset = sections.addSection(2, annotationSection.encode())
		return relativizeAddresses(annotationStringRecordOffsets, sectionOffset)
	
	def storeMesh(meshSection, meshSectionAddress, mesh, annotations, geometryAddresses, annotationStringAddresses, boneGroupAddresses, materialAddresses):
		boneGroupArray = RecordArray(4)
		if mesh.boneGroup is not None:
			boneGroupOffset = boneGroupAddresses[mesh.boneGroup] - meshSectionAddress
			boneGroupArray.addRecord(pack('< i', boneGroupOffset))
		boneGroupRecordOffset = meshSection.addBlob(boneGroupArray.encode())
		
		if len(annotations) == 0:
			annotationsArrayOffset = 0
		else:
			annotationsArray = RecordArray(16)
			for (annotationType, annotationString) in annotations:
				annotationStringOffset = annotationStringAddresses[annotationString] - meshSectionAddress
				annotationsArray.addRecord(pack('< 4i',
					annotationStringOffset,
					0, # section3Offset
					7, # unknown
					annotationType,
				))
			annotationsArrayOffset = meshSection.addBlob(annotationsArray.encode())
		
		meshSection.addRecord(pack('< i II i II',
			geometryAddresses[mesh] - meshSectionAddress,
			boneGroupRecordOffset,
			annotationsArrayOffset,
			materialAddresses[mesh.material] - meshSectionAddress,
			0, # section10Offset
			0, # unknownDataOffset
		))
	
	def storeMeshes(sections, meshes, boneGroupAddresses, materialAddresses, modelExtensionHeaders):
		geometrySection = StructArray(20, len(meshes))
		meshSection = StructArray(24, len(meshes))
		
		geometryOffsets = {}
		meshAnnotations = {}
		
		for mesh in meshes:
			geometryOffset = storeMeshGeometry(geometrySection, mesh)
			geometryOffsets[mesh] = geometryOffset
			
			meshAnnotations[mesh] = getMeshAnnotations(mesh)
		
		geometrySectionOffset = sections.addSection(1, geometrySection.encode())
		geometryAddresses = relativizeAddresses(geometryOffsets, geometrySectionOffset)
		
		annotationStringAddresses = storeAnnotations(sections, meshAnnotations, modelExtensionHeaders)
		
		meshSectionAddress = sections.nextSectionAddress()
		
		for mesh in meshes:
			storeMesh(
				meshSection,
				meshSectionAddress,
				mesh,
				meshAnnotations[mesh],
				geometryAddresses,
				annotationStringAddresses,
				boneGroupAddresses,
				materialAddresses,
			)
		
		sections.addSection(4, meshSection.encode())
	
	def storeDummySections(sections):
		# Suspected editor-only data, in numeric form
		sections.addSection(3, StructArray(28, 0).encode())
		
		# Cloth physics data
		sections.addSection(8, StructArray(4, 0).encode())
		
		# Material combination data
		sections.addSection(9, StructArray(4, 0).encode())
		
		# Locator geometry
		sections.addSection(10, StructArray(16, 0, pack('< I', 0)).encode())
	
	
	sections = Sections(11)
	
	storeMetadata(sections, model)
	boneGroupAddresses = storeBones(sections, model.bones, [mesh.boneGroup for mesh in model.meshes if mesh.boneGroup is not None])
	materialAddresses = storeMaterials(sections, model.materials)
	storeDummySections(sections)
	storeMeshes(sections, model.meshes, boneGroupAddresses, materialAddresses, model.extensionHeaders)
	
	return sections.encode()

def writeModelFile(model, filename):
	data = writeModel(model)
	with open(filename, 'wb') as stream:
		stream.write(data)



import sys
if __name__ == '__main__':
	readModelFile(sys.argv[1])
