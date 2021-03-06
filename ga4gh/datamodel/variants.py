"""
Module responsible for translating variant data into GA4GH native
objects.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import glob
import datetime

import pysam
import wormtable as wt

import ga4gh.protocol as protocol


class VariantSet(object):
    """
    Class representing a single VariantSet in the GA4GH data model.
    """
    # TODO abstract details shared by wormtable and tabix based backends.


class WormtableVariantSet(VariantSet):
    """
    Class representing a single variant set backed by a wormtable directory.
    We assume that VCF data has been converted to wormtable format using
    vcf2wt, which maps VCF data to wormtable columns in the following
    way:
    o The zeroth column (row_id) in a row is the primary key;
    o The next 7 columns correspond to the fixed fields in VCF, and are
      called CHROM, POS, etc.
    o There is then a single column for each INFO field defined in the
      VCF header. The column names are prefixed with "INFO.", and so
      we might have INFO.AF, INFO.THETA etc.
    o There is then a set of columns for each sample, one for each field
      defined in the FORMAT rows of the header. These are prefixed by the
      sample ID and suffixed with the field name, and so we have fields
      like HG00096.GT, NA20828.GL etc.
    The ``wtadmin show`` command is useful to see the columns in a table.

    We assume that indexes on the combination of the CHROM and POS columns
    and the CHROM and ID columns have been built. This is to support
    efficient retrieval of rows based on both coordinates and names
    within a chromosome.
    """
    # Positions of the fixed columns within a row
    CHROM_COL = 1
    POS_COL = 2
    ID_COL = 3
    REF_COL = 4
    ALT_COL = 5
    QUAL_COL = 6
    FILTER_COL = 7
    # These must be bytes literals for integration with wormtable.
    GENOTYPE_LIKELIHOOD_NAME = b"GL"
    GENOTYPE_NAME = b"GT"

    def __init__(self, variantSetId, wtDir):
        """
        Allocates a new WormtableVariantSet with the specified variantSetId
        based on the specified wormtable directory.
        """
        self._variantSetId = variantSetId
        self._wtDir = wtDir
        self._table = wt.open_table(wtDir)
        self._chromPosIndex = self._table.open_index("CHROM+POS")
        self._chromIdIndex = self._table.open_index("CHROM+ID")
        self._sampleCols = {}
        self._sampleNames = []
        self._infoCols = []
        self._firstSamplePosition = -1
        ctimeInMillis = int(os.path.getctime(wtDir) * 1000)
        # ctime is in seconds, and we want milliseconds since the epoch
        self._creationTime = ctimeInMillis
        self._updatedTime = ctimeInMillis
        cols = self._table.columns()[self.FILTER_COL + 1:]
        # We build lookup tables for the INFO and sample columns so they can
        # be easily found during conversion. For the sample columns we make
        # a dictionary mapping the sample name to a list of (sample name, col)
        # tuples for that sample.
        for col in cols:
            colName = col.get_name()
            if colName.startswith("INFO"):
                infoField = colName.split(".")[1]
                self._infoCols.append((infoField, col))
            else:
                if self._firstSamplePosition == -1:
                    # This must be a sample specific column
                    self._firstSamplePosition = col.get_position()
                sampleName, infoName = colName.split(".")
                if sampleName not in self._sampleCols:
                    self._sampleCols[sampleName] = []
                    self._sampleNames.append(sampleName)
                self._sampleCols[sampleName].append((infoName, col))

    def convertInfoField(self, value):
        """
        Converts the specified value into an appropriate format for a protocol
        info field. Info fields are lists of strings.
        """
        if isinstance(value, tuple):
            ret = [str(x) for x in value]
        else:
            ret = [str(value)]
        return ret

    def convertGenotype(self, call, genotype):
        """
        Updates the specified call to reflect the value encoded in the
        specified VCF genotype string.
        """
        if genotype is not None:
            # Is this the correct interpretation of | and /?
            delim = "/"
            if "|" in genotype:
                delim = "|"
                # TODO what is the phaseset value supposed to be?
                call.phaseset = "True"
            try:
                call.genotype = map(int, genotype.split(delim))
            except ValueError:
                # TODO what is the correct interpretation of .|.?
                call.genotype = [-1]

    def convertVariant(self, row, sampleRowPositions):
        """
        Converts the specified wormtable row into a GAVariant object including
        the specified set of callSetIds.
        """
        variant = protocol.GAVariant()
        variant.created = self._creationTime
        variant.updated = self._updatedTime
        variant.variantSetId = self._variantSetId
        variant.referenceName = row[self.CHROM_COL]
        variant.start = row[self.POS_COL]
        names = row[self.ID_COL]
        if names is not None:
            variant.names = names.split(";")
        variant.referenceBases = row[self.REF_COL]
        variant.end = variant.start + len(variant.referenceBases)
        variant.id = "{0}.{1}".format(variant.variantSetId, row[0])
        alt = row[self.ALT_COL]
        if alt is not None:
            variant.alternateBases = alt.split(",")
        variant.info = {}
        for infoField, col in self._infoCols:
            pos = col.get_position()
            if row[pos] is not None:
                variant.info[infoField] = self.convertInfoField(row[pos])
        # All of the remaining values in the row correspond to Calls.
        for callSetId, rowPositions in sampleRowPositions.items():
            call = protocol.GACall()
            # Why do we have both of these? Wouldn't the ID be sufficient
            # to call back to searchCallSets to get more info?
            call.callSetId = callSetId
            call.callSetName = callSetId
            for (info, col), rowPosition in zip(self._sampleCols[callSetId],
                                                rowPositions):
                if info == self.GENOTYPE_LIKELIHOOD_NAME:
                    call.genotypeLikelihood = row[rowPosition]
                elif info == self.GENOTYPE_NAME:
                    self.convertGenotype(call, row[rowPosition])
                else:
                    if row[rowPosition] is not None:
                        # Missing values are not included in the info array
                        call.info[info] = self.convertInfoField(
                            row[rowPosition])
            variant.calls.append(call)
        return variant

    def getVariants(self, referenceName, startPosition, endPosition,
                    variantName, callSetIds):
        """
        Returns an iterator over the specified variants. The parameters
        correspond to the attributes of a GASearchVariantsRequest object.
        """
        # TODO encoding issues here? Wormtable (and most likely pysam and other
        # low level libraries) only deal with bytes. We'll need to adopt some
        # convention here for where we move from unicode to bytes.
        chrom = referenceName.encode()
        if callSetIds is None:
            readCols = self._table.columns()
            # These must be in the correct order, so we cannot use the keys of
            # self._sampleCols
            callSetIds = self._sampleNames
        else:
            readCols = self._table.columns()[:self._firstSamplePosition]
            for callSetId in callSetIds:
                cols = [col for name, col in self._sampleCols[callSetId]]
                readCols.extend(cols)
        # Now we get the row positions for the sample columns
        sampleRowPositions = {}
        currentRowPosition = self._firstSamplePosition
        for callSetId in callSetIds:
            sampleRowPositionsList = []
            for col in self._sampleCols[callSetId]:
                sampleRowPositionsList.append(currentRowPosition)
                currentRowPosition += 1
            sampleRowPositions[callSetId] = sampleRowPositionsList
        if variantName is None:
            cursor = self._chromPosIndex.cursor(
                readCols, (chrom, startPosition), (chrom, endPosition))
            for row in cursor:
                yield self.convertVariant(row, sampleRowPositions)
        else:
            variantName = variantName.encode()  # TODO encoding?
            cursor = self._chromIdIndex.cursor(readCols, (chrom, variantName))
            for row in cursor:
                # TODO issues with having several names for a variant?
                # The result must still be within the range and must match
                # the specified name exactly. The cursor is positioned at
                # the first row >= the specified key.
                if (startPosition <= row[self.POS_COL] < endPosition and
                        row[self.ID_COL] == variantName):
                    yield self.convertVariant(row, sampleRowPositions)
                else:
                    break

    def getMetadata(self):
        """
        Returns a list of GAVariantSetMetadata objects for this variant set.
        """
        def buildMetadata(infoField, col):
            metadata = protocol.GAVariantSetMetadata()
            metadata.key = infoField
            metadata.value = ""
            metadata.id = ""
            # What are the encodings here? With the lack of any precise
            # definitions I'm just using wormtable values for now.
            metadata.type = col.get_type_name()
            metadata.number = str(col.get_num_elements())
            metadata.description = col.get_description()
            return metadata
        ret = []
        for infoField, col in self._infoCols:
            ret.append(buildMetadata(infoField, col))
        if len(self._sampleCols) > 0:
            # TODO this is pretty nasty, making a list just to take the head.
            sampleName = list(self._sampleCols.keys())[0]
            for infoField, col in self._sampleCols[sampleName]:
                if infoField != self.GENOTYPE_NAME:
                    ret.append(buildMetadata(infoField, col))
        return ret


class TabixVariantSet(VariantSet):
    """
    Class representing a single variant set backed by a tabix directory.
    """
    def __init__(self, variantSetId, vcfPath):
        self._variantSetId = variantSetId
        self._created = protocol.convertDatetime(datetime.datetime.now())
        self._chromTabixFileMap = {}
        for vcfFile in glob.glob(os.path.join(vcfPath, "*.vcf.gz")):
            tabixFile = pysam.Tabixfile(vcfFile)
            for chrom in tabixFile.contigs:
                if chrom in self._chromTabixFileMap:
                    raise Exception("cannot have overlapping VCF files.")
                self._chromTabixFileMap[chrom] = tabixFile

    def convertVariant(self, record):
        """
        Converts the specified pysam VCF Record into a GA4GH GAVariant
        object.
        """
        variant = protocol.GAVariant()
        record = record.split('\t')
        position = int(record[1])
        variant.id = "{0}:{1}:{2}".format(self._variantSetId,
                                          record[0], position)
        variant.variantSetId = self._variantSetId
        variant.referenceName = record[0]
        variant.names = []
        variant.created = self._created
        variant.updated = self._created
        variant.start = position
        variant.end = position + 1  # TODO support non SNP variants
        variant.referenceBases = record[3]
        variant.alternateBases = [record[4].split(",")]
        for j in range(9, len(record)):
            c = protocol.GACall()
            c.genotype = record[j].split(":")[0].split("[\/\|]")
            variant.calls.append(c)
        return variant

    def getVariants(self, referenceName, startPosition, endPosition,
                    variantName, callSetIds):
        """
        Returns an iterator over the specified variants. The parameters
        correspond to the attributes of a GASearchVariantsRequest object.
        """
        if variantName is not None:
            raise NotImplementedError(
                "Searching by variantName is not supported")
        if len(callSetIds) != 0:
            raise NotImplementedError(
                "Specifying call set ids is not supported")
        if referenceName in self._chromTabixFileMap:
            tabixFile = self._chromTabixFileMap[referenceName]
            cursor = tabixFile.fetch(referenceName, startPosition, endPosition)
            for record in cursor:
                yield self.convertVariant(record)

    def getMetadata(self):
        # TODO: Implement this
        ret = []
        return ret
