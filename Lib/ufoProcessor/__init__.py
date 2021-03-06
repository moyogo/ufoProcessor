# coding: utf-8


from __future__ import print_function, division, absolute_import

import plistlib
import os
import logging, traceback
import collections
from pprint import pprint

from fontTools.designspaceLib import DesignSpaceDocument, SourceDescriptor, InstanceDescriptor, AxisDescriptor, RuleDescriptor, processRules
from fontTools.varLib.models import VariationModel, normalizeLocation

from ufoLib import fontInfoAttributesVersion1, fontInfoAttributesVersion2, fontInfoAttributesVersion3

import defcon
from defcon.objects.font import Font
from defcon.pens.transformPointPen import TransformPointPen
from defcon.objects.component import _defaultTransformation
from fontMath.mathGlyph import MathGlyph
from fontMath.mathInfo import MathInfo
from fontMath.mathKerning import MathKerning

# if you only intend to use varLib.model then importing mutatorMath is not necessary.
from mutatorMath.objects.mutator import buildMutator
from ufoProcessor.varModels import VariationModelMutator


class UFOProcessorError(Exception):
    def __init__(self, msg, obj=None):
        self.msg = msg
        self.obj = obj

    def __str__(self):
        return repr(self.msg) + repr(self.obj)


"""
    Processing of rules when generating UFOs.
    Swap the contents of two glyphs.
        - contours
        - components
        - width
        - group membership
        - kerning

    + Remap components so that glyphs that reference either of the swapped glyphs maintain appearance
    + Keep the unicode value of the original glyph.
    
    Notes
    Parking the glyphs under a swapname is a bit lazy, but at least it guarantees the glyphs have the right parent.

"""


""" 
    build() is a convenience function for reading and executing a designspace file.
        documentPath: path to the designspace file.
        outputUFOFormatVersion: integer, 2, 3. Format for generated UFOs. Note: can be different from source UFO format.
        useVarlib: True if you want the geometry to be generated with varLib.model instead of mutatorMath.
"""

def build(
        documentPath,
        outputUFOFormatVersion=3,
        roundGeometry=True,
        verbose=True,           # not supported
        logPath=None,           # not supported
        progressFunc=None,      # not supported
        processRules=True,
        logger=None,
        useVarlib=False,
        ):
    """
        Simple builder for UFO designspaces.
    """
    import os, glob
    if os.path.isdir(documentPath):
        # process all *.designspace documents in this folder
        todo = glob.glob(os.path.join(documentPath, "*.designspace"))
    else:
        # process the 
        todo = [documentPath]
    results = []
    for path in todo:
        document = DesignSpaceProcessor(ufoVersion=outputUFOFormatVersion)
        document.useVarlib = useVarlib
        document.roundGeometry = roundGeometry
        document.read(path)
        try:
            r = document.generateUFO(processRules=processRules)
            results.append(r)
        except:
            if logger:
                logger.exception("ufoProcessor error")
        #results += document.generateUFO(processRules=processRules)
        reader = None
    return results


def getUFOVersion(ufoPath):
    # Peek into a ufo to read its format version. 
            # <?xml version="1.0" encoding="UTF-8"?>
            # <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            # <plist version="1.0">
            # <dict>
            #   <key>creator</key>
            #   <string>org.robofab.ufoLib</string>
            #   <key>formatVersion</key>
            #   <integer>2</integer>
            # </dict>
            # </plist>
    metaInfoPath = os.path.join(ufoPath, u"metainfo.plist")
    p = plistlib.readPlist(metaInfoPath)
    return p.get('formatVersion')


def swapGlyphNames(font, oldName, newName, swapNameExtension = "_______________swap"):
    # In font swap the glyphs oldName and newName.
    # Also swap the names in components in order to preserve appearance.
    # Also swap the names in font groups. 
    if not oldName in font or not newName in font:
        return None
    swapName = oldName + swapNameExtension
    # park the old glyph 
    if not swapName in font:
        font.newGlyph(swapName)
    # swap the outlines
    font[swapName].clear()
    p = font[swapName].getPointPen()
    font[oldName].drawPoints(p)
    font[swapName].width = font[oldName].width
    # lib?
    font[oldName].clear()
    p = font[oldName].getPointPen()
    font[newName].drawPoints(p)
    font[oldName].width = font[newName].width
    
    font[newName].clear()
    p = font[newName].getPointPen()
    font[swapName].drawPoints(p)
    font[newName].width = font[swapName].width
    
    # remap the components
    for g in font:
        for c in g.components:
           if c.baseGlyph == oldName:
               c.baseGlyph = swapName
           continue
    for g in font:
        for c in g.components:
           if c.baseGlyph == newName:
               c.baseGlyph = oldName
           continue
    for g in font:
        for c in g.components:
           if c.baseGlyph == swapName:
               c.baseGlyph = newName
   
    # change the names in groups
    # the shapes will swap, that will invalidate the kerning
    # so the names need to swap in the kerning as well.
    newKerning = {}
    for first, second in font.kerning.keys():
        value = font.kerning[(first,second)]
        if first == oldName:
            first = newName
        elif first == newName:
            first = oldName
        if second == oldName:
            second = newName
        elif second == newName:
            second = oldName
        newKerning[(first, second)] = value
    font.kerning.clear()
    font.kerning.update(newKerning)
            
    for groupName, members in font.groups.items():
        newMembers = []
        for name in members:
            if name == oldName:
                newMembers.append(newName)
            elif name == newName:
                newMembers.append(oldName)
            else:
                newMembers.append(name)
        font.groups[groupName] = newMembers
    
    remove = []
    for g in font:
        if g.name.find(swapNameExtension)!=-1:
            remove.append(g.name)
    for r in remove:
        del font[r]


class DecomposePointPen(object):
    
    def __init__(self, glyphSet, outPointPen):
        self._glyphSet = glyphSet
        self._outPointPen = outPointPen
        self.beginPath = outPointPen.beginPath
        self.endPath = outPointPen.endPath
        self.addPoint = outPointPen.addPoint
        
    def addComponent(self, baseGlyphName, transformation):
        if baseGlyphName in self._glyphSet:
            baseGlyph = self._glyphSet[baseGlyphName]
            if transformation == _defaultTransformation:
                baseGlyph.drawPoints(self)
            else:
                transformPointPen = TransformPointPen(self, transformation)
                baseGlyph.drawPoints(transformPointPen)



class DesignSpaceProcessor(DesignSpaceDocument):
    """
        A subclassed DesignSpaceDocument that can
            - process the document and generate finished UFOs with MutatorMath or varLib.model.
            - read and write documents
            - Replacement for the mutatorMath.ufo generator.
    """

    fontClass = defcon.Font
    glyphClass = defcon.Glyph
    libClass = defcon.Lib
    glyphContourClass = defcon.Contour
    glyphPointClass = defcon.Point
    glyphComponentClass = defcon.Component
    glyphAnchorClass = defcon.Anchor
    kerningClass = defcon.Kerning
    groupsClass = defcon.Groups
    infoClass = defcon.Info
    featuresClass = defcon.Features

    mathInfoClass = MathInfo
    mathGlyphClass = MathGlyph
    mathKerningClass = MathKerning

    def __init__(self, readerClass=None, writerClass=None, fontClass=None, ufoVersion=3, useVarlib=False):
        super(DesignSpaceProcessor, self).__init__(readerClass=readerClass, writerClass=writerClass)

        self.ufoVersion = ufoVersion         # target UFO version
        self.useVarlib = useVarlib
        self.roundGeometry = False
        self._glyphMutators = {}
        self._infoMutator = None
        self._kerningMutator = None
        self.fonts = {}
        self._fontsLoaded = False
        self.glyphNames = []     # list of all glyphnames
        self.processRules = True
        self.problems = []  # receptacle for problem notifications. Not big enough to break, but also not small enough to ignore.
        if readerClass is not None:
            print("ufoProcessor.ruleDescriptorClass", readerClass.ruleDescriptorClass)

    def generateUFO(self, processRules=True):
        # makes the instances
        # option to execute the rules
        # make sure we're not trying to overwrite a newer UFO format
        self.loadFonts()
        self.findDefault()
        if self.default is None:
            # we need one to genenerate
            raise UFOProcessorError("Can't generate UFO from this designspace: no default font.", self)
        v = 0
        for instanceDescriptor in self.instances:
            if instanceDescriptor.path is None:
                continue
            font = self.makeInstance(instanceDescriptor, processRules)
            folder = os.path.dirname(instanceDescriptor.path)
            path = instanceDescriptor.path
            if not os.path.exists(folder):
                os.makedirs(folder)
            if os.path.exists(path):
                existingUFOFormatVersion = getUFOVersion(path)
                if existingUFOFormatVersion > self.ufoVersion:
                    self.problems.append(u"Can’t overwrite existing UFO%d with UFO%d." % (existingUFOFormatVersion, self.ufoVersion))
                    continue
            font.save(path, self.ufoVersion)
            self.problems.append("Generated %s as UFO%d"%(os.path.basename(path), self.ufoVersion))
        return True

    def getSerializedAxes(self):
        return [a.serialize() for a in self.axes]

    def getMutatorAxes(self):
        d = collections.OrderedDict()

        for a in self.axes:
            d[a.name] = a.serialize()
        return d

    serializedAxes = property(getSerializedAxes, doc="a list of dicts with the axis values")

    def getVariationModel(self, items, axes, bias=None):
        # Return either a mutatorMath or a varlib.model object for calculating. 
        try:
            if self.useVarlib:
                # use the varlib variation model
                return dict(), VariationModelMutator(items, self.axes)
            else:
                # use mutatormath model
                axesForMutator = self.getMutatorAxes()
                return buildMutator(items, axes=axesForMutator, bias=bias)
        except:
            error = traceback.format_exc()
            self.problems.append("UFOProcessor.getVariationModel error: %s" % error)
            return None

    def getInfoMutator(self):
        """ Returns a info mutator """
        if self._infoMutator:
            return self._infoMutator
        infoItems = []
        for sourceDescriptor in self.sources:
            loc = sourceDescriptor.location
            sourceFont = self.fonts[sourceDescriptor.name]
            infoItems.append((loc, self.mathInfoClass(sourceFont)))
        bias, self._infoMutator = self.getVariationModel(infoItems, axes=self.serializedAxes, bias=self.defaultLoc)
        return self._infoMutator

    def getKerningMutator(self):
        """ Return a kerning mutator, collect the sources, build mathGlyphs. """
        if self._kerningMutator:
            return self._kerningMutator
        kerningItems = []
        for sourceDescriptor in self.sources:
            loc = sourceDescriptor.location
            sourceFont = self.fonts[sourceDescriptor.name]
            # this makes assumptions about the groups of all sources being the same. 
            kerningItems.append((loc, self.mathKerningClass(sourceFont.kerning, sourceFont.groups)))
        bias, self._kerningMutator = self.getVariationModel(kerningItems, axes=self.serializedAxes, bias=self.defaultLoc)
        return self._kerningMutator

    def getGlyphMutator(self, glyphName, decomposeComponents=False, fromCache=True):
        cacheKey = (glyphName, decomposeComponents)
        if cacheKey in self._glyphMutators and fromCache:
            return self._glyphMutators[cacheKey]
        items = self.collectMastersForGlyph(glyphName, decomposeComponents=decomposeComponents)
        new = []
        for a, b, c in items:
            if hasattr(b, "toMathGlyph"):
                new.append((a,b.toMathGlyph()))
            else:
                new.append((a,self.mathGlyphClass(b)))
        items = new
        bias, thing = self.getVariationModel(items, axes=self.serializedAxes, bias=self.defaultLoc)
        self._glyphMutators[cacheKey] = thing
        return thing

    def collectMastersForGlyph(self, glyphName, decomposeComponents=False):
        """ Return a glyph mutator.defaultLoc
            decomposeComponents = True causes the source glyphs to be decomposed first
            before building the mutator. That gives you instances that do not depend
            on a complete font. If you're calculating previews for instance.

            XXX check glyphs in layers
        """
        items = []
        for sourceDescriptor in self.sources:
            loc = sourceDescriptor.location
            f = self.fonts[sourceDescriptor.name]
            sourceLayer = f
            if glyphName in sourceDescriptor.mutedGlyphNames:
                continue
            if not glyphName in f:
                # log this>
                continue
            layerName = "foreground"
            # handle source layers

            if sourceDescriptor.layerName is not None:
                # start looking for a layer
                if sourceDescriptor.layerName in f.layers:
                    sourceLayer = f.layers[sourceDescriptor.layerName]
                    layerName = sourceDescriptor.layerName
                    # start looking for a glyph
                    if glyphName not in sourceLayer:
                        # this might be a support in a sparse layer
                        # so we're skipping!
                        #print("XXXX", glyphName, "not in", sourceDescriptor.layerName)
                        continue
            sourceGlyphObject = sourceLayer[glyphName]
            if decomposeComponents:
                # what about decomposing glyphs in a partial font?
                temp = self.glyphClass()
                p = temp.getPointPen()
                dpp = DecomposePointPen(sourceLayer, p)
                sourceGlyphObject.drawPoints(dpp)
                temp.width = sourceGlyphObject.width
                temp.name = sourceGlyphObject.name
                #temp.lib = sourceGlyphObject.lib
                processThis = temp
            else:
                processThis = sourceGlyphObject
            sourceInfo = dict(source=f.path, glyphName=glyphName, layerName=layerName, location=sourceDescriptor.location, sourceName=sourceDescriptor.name)
            if hasattr(processThis, "toMathGlyph"):
                processThis = processThis.toMathGlyph()
            else:
                processThis = self.mathGlyphClass(processThis)
            items.append((loc, processThis, sourceInfo))
        return items

    def getNeutralFont(self):
        # Return a font object for the neutral font
        # self.fonts[self.default.name] ?
        neutralLoc = self.newDefaultLocation()
        for sd in self.sources:
            if sd.location == neutralLoc:
                if sd.name in self.fonts:
                    return self.fonts[sd.name]
        return None

    def loadFonts(self, reload=False):
        # Load the fonts and find the default candidate based on the info flag
        if self._fontsLoaded and not reload:
            return
        names = set()
        for sourceDescriptor in self.sources:
            if not sourceDescriptor.name in self.fonts:
                if os.path.exists(sourceDescriptor.path):
                    self.fonts[sourceDescriptor.name] = self._instantiateFont(sourceDescriptor.path)
                    self.problems.append("loaded master from %s, format %d" % (sourceDescriptor.path, getUFOVersion(sourceDescriptor.path)))
                    names = names | set(self.fonts[sourceDescriptor.name].keys())
                else:
                    self.fonts[sourceDescriptor.name] = None
                    self.problems.append("source ufo not found at %s" % (sourceDescriptor.path))
        self.glyphNames = list(names)
        self._fontsLoaded = True

    def getFonts(self):
        # returnn a list of (font object, location) tuples
        fonts = []
        for sourceDescriptor in self.sources:
            f = self.fonts.get(sourceDescriptor.name)
            if f is not None:
                fonts.append((f, sourceDescriptor.location))
        return fonts

    def makeInstance(self, instanceDescriptor, doRules=False, glyphNames=None):
        """ Generate a font object for this instance """
        font = self._instantiateFont(None)
        # make fonty things here
        loc = instanceDescriptor.location
        anisotropic = False
        locHorizontal = locVertical = loc
        if self.isAnisotropic(loc):
            anisotropic = True
            locHorizontal, locVertical = self.splitAnisotropic(loc)
        # groups
        if hasattr(self.fonts[self.default.name], "kerningGroupConversionRenameMaps"):
            renameMap = self.fonts[self.default.name].kerningGroupConversionRenameMaps
        else:
            renameMap = {}
        font.kerningGroupConversionRenameMaps = renameMap
        # make the kerning
        # this kerning is always horizontal. We can take the horizontal location
        if instanceDescriptor.kerning:
            try:
                kerningMutator = self.getKerningMutator()
                kerningObject = kerningMutator.makeInstance(locHorizontal)
                kerningObject.extractKerning(font)
            except:
                self.problems.append("Could not make kerning for %s. %s" % (loc, traceback.format_exc()))
        # make the info
        try:
            infoMutator = self.getInfoMutator()
            if not anisotropic:
                infoInstanceObject = infoMutator.makeInstance(loc)
            else:
                horizontalInfoInstanceObject = infoMutator.makeInstance(locHorizontal)
                verticalInfoInstanceObject = infoMutator.makeInstance(locVertical)
                # merge them again
                infoInstanceObject = (1,0)*horizontalInfoInstanceObject + (0,1)*verticalInfoInstanceObject
            infoInstanceObject.extractInfo(font.info)
            font.info.familyName = instanceDescriptor.familyName
            font.info.styleName = instanceDescriptor.styleName
            font.info.postScriptFontName = instanceDescriptor.postScriptFontName
            font.info.styleMapFamilyName = instanceDescriptor.styleMapFamilyName
            font.info.styleMapStyleName = instanceDescriptor.styleMapStyleName
            # NEED SOME HELP WITH THIS
            # localised names need to go to the right openTypeNameRecords
            # records = []
            # nameID = 1
            # platformID = 
            # for languageCode, name in instanceDescriptor.localisedStyleMapFamilyName.items():
            #    # Name ID 1 (font family name) is found at the generic styleMapFamily attribute.
            #    records.append((nameID, ))
        except:
            self.problems.append("Could not make fontinfo for %s. %s" % (loc, traceback.format_exc()))
        for sourceDescriptor in self.sources:
            if sourceDescriptor.copyInfo:
                # this is the source
                self._copyFontInfo(self.fonts[sourceDescriptor.name].info, font.info)
            if sourceDescriptor.copyLib:
                # excplicitly copy the font.lib items
                for key, value in self.fonts[sourceDescriptor.name].lib.items():
                    font.lib[key] = value
            if sourceDescriptor.copyFeatures:
                featuresText = self.fonts[sourceDescriptor.name].features.text
                if isinstance(featuresText, str):
                    font.features.text = u""+featuresText
                elif isinstance(featuresText, unicode):
                    font.features.text = featuresText
        # glyphs
        if glyphNames:
            selectedGlyphNames = glyphNames
        else:
            selectedGlyphNames = self.glyphNames
        # add the glyphnames to the font.lib['public.glyphOrder']
        if not 'public.glyphOrder' in font.lib.keys():
            font.lib['public.glyphOrder'] = selectedGlyphNames
        for glyphName in selectedGlyphNames:
            try:
                glyphMutator = self.getGlyphMutator(glyphName)
                if glyphMutator is None:
                    continue
            except:
                self.problems.append("Could not make mutator for glyph %s %s" % (glyphName, traceback.format_exc()))
                continue
            if glyphName in instanceDescriptor.glyphs.keys():
                # XXX this should be able to go now that we have full rule support. 
                # reminder: this is what the glyphData can look like
                # {'instanceLocation': {'custom': 0.0, 'weight': 824.0},
                #  'masters': [{'font': 'master.Adobe VF Prototype.Master_0.0',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 0.0, 'weight': 0.0}},
                #              {'font': 'master.Adobe VF Prototype.Master_1.1',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 0.0, 'weight': 368.0}},
                #              {'font': 'master.Adobe VF Prototype.Master_2.2',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 0.0, 'weight': 1000.0}},
                #              {'font': 'master.Adobe VF Prototype.Master_3.3',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 100.0, 'weight': 1000.0}},
                #              {'font': 'master.Adobe VF Prototype.Master_0.4',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 100.0, 'weight': 0.0}},
                #              {'font': 'master.Adobe VF Prototype.Master_4.5',
                #               'glyphName': 'dollar.nostroke',
                #               'location': {'custom': 100.0, 'weight': 368.0}}],
                #  'unicodes': [36]}
                glyphData = instanceDescriptor.glyphs[glyphName]
            else:
                glyphData = {}
            font.newGlyph(glyphName)
            font[glyphName].clear()
            if glyphData.get('mute', False):
                # mute this glyph, skip
                continue
            glyphInstanceLocation = glyphData.get("instanceLocation", instanceDescriptor.location)
            uniValues = []
            neutral = glyphMutator.get(())
            if neutral is not None:
                uniValues = neutral[0].unicodes
            glyphInstanceUnicodes = glyphData.get("unicodes", uniValues)
            note = glyphData.get("note")
            if note:
                font[glyphName] = note
            masters = glyphData.get("masters", None)
            if masters:
                items = []
                for glyphMaster in masters:
                    sourceGlyphFont = glyphMaster.get("font")
                    sourceGlyphName = glyphMaster.get("glyphName", glyphName)
                    m = self.fonts.get(sourceGlyphFont)
                    if not sourceGlyphName in m:
                        continue
                    if hasattr(m[sourceGlyphName], "toMathGlyph"):
                        sourceGlyph = m[sourceGlyphName].toMathGlyph()
                    else:
                        sourceGlyph = MathGlyph(m[sourceGlyphName])
                    sourceGlyphLocation = glyphMaster.get("location")
                    items.append((sourceGlyphLocation, sourceGlyph))
                bias, glyphMutator = self.getVariationModel(items, axes=self.serializedAxes, bias=self.defaultLoc)
            try:
                if not self.isAnisotropic(glyphInstanceLocation):
                    glyphInstanceObject = glyphMutator.makeInstance(glyphInstanceLocation)
                else:
                    # split anisotropic location into horizontal and vertical components
                    horizontal, vertical = self.splitAnisotropic(glyphInstanceLocation)
                    horizontalGlyphInstanceObject = glyphMutator.makeInstance(horizontal)
                    verticalGlyphInstanceObject = glyphMutator.makeInstance(vertical)
                    # merge them again
                    glyphInstanceObject = (0,1)*horizontalGlyphInstanceObject + (1,0)*verticalGlyphInstanceObject
            except IndexError:
                # alignment problem with the data?
                print("Error making instance %s" % glyphName)
                continue
            font.newGlyph(glyphName)
            font[glyphName].clear()
            if self.roundGeometry:
                try:
                    glyphInstanceObject = glyphInstanceObject.round()
                except AttributeError:
                    pass
            try:
                glyphInstanceObject.extractGlyph(font[glyphName], onlyGeometry=True)
            except TypeError:
                # this causes ruled glyphs to end up in the wrong glyphname
                # but defcon2 objects don't support it
                pPen = font[glyphName].getPointPen()
                font[glyphName].clear()
                glyphInstanceObject.drawPoints(pPen)
            font[glyphName].width = glyphInstanceObject.width
            font[glyphName].unicodes = glyphInstanceUnicodes
        if doRules:
            resultNames = processRules(self.rules, loc, self.glyphNames)
            for oldName, newName in zip(self.glyphNames, resultNames):
                if oldName != newName:
                    swapGlyphNames(font, oldName, newName)
        # copy the glyph lib?
        #for sourceDescriptor in self.sources:
        #    if sourceDescriptor.copyLib:
        #        pass
        #    pass
        # store designspace location in the font.lib
        font.lib['designspace'] = list(instanceDescriptor.location.items())
        return font

    def isAnisotropic(self, location):
        for v in location.values():
            if type(v)==tuple:
                return True
        return False

    def splitAnisotropic(self, location):
        x = {}
        y = {}
        for dim, val in location.items():
            if type(val)==tuple:
                x[dim] = val[0]
                y[dim] = val[1]
            else:
                x[dim] = y[dim] = val
        return x, y

    def _instantiateFont(self, path):
        """ Return a instance of a font object with all the given subclasses"""
        try:
            return self.fontClass(path,
                libClass=self.libClass,
                kerningClass=self.kerningClass,
                groupsClass=self.groupsClass,
                infoClass=self.infoClass,
                featuresClass=self.featuresClass,
                glyphClass=self.glyphClass,
                glyphContourClass=self.glyphContourClass,
                glyphPointClass=self.glyphPointClass,
                glyphComponentClass=self.glyphComponentClass,
                glyphAnchorClass=self.glyphAnchorClass)
        except TypeError:
            # if our fontClass doesnt support all the additional classes
            return self.fontClass(path)

    def _copyFontInfo(self, sourceInfo, targetInfo):
        """ Copy the non-calculating fields from the source info."""
        infoAttributes = [
            "versionMajor",
            "versionMinor",
            "copyright",
            "trademark",
            "note",
            "openTypeGaspRangeRecords",
            "openTypeHeadCreated",
            "openTypeHeadFlags",
            "openTypeNameDesigner",
            "openTypeNameDesignerURL",
            "openTypeNameManufacturer",
            "openTypeNameManufacturerURL",
            "openTypeNameLicense",
            "openTypeNameLicenseURL",
            "openTypeNameVersion",
            "openTypeNameUniqueID",
            "openTypeNameDescription",
            "#openTypeNamePreferredFamilyName",
            "#openTypeNamePreferredSubfamilyName",
            "#openTypeNameCompatibleFullName",
            "openTypeNameSampleText",
            "openTypeNameWWSFamilyName",
            "openTypeNameWWSSubfamilyName",
            "openTypeNameRecords",
            "openTypeOS2Selection",
            "openTypeOS2VendorID",
            "openTypeOS2Panose",
            "openTypeOS2FamilyClass",
            "openTypeOS2UnicodeRanges",
            "openTypeOS2CodePageRanges",
            "openTypeOS2Type",
            "postscriptIsFixedPitch",
            "postscriptForceBold",
            "postscriptDefaultCharacter",
            "postscriptWindowsCharacterSet"
        ]
        for infoAttribute in infoAttributes:
            copy = False
            if self.ufoVersion == 1 and infoAttribute in fontInfoAttributesVersion1:
                copy = True
            elif self.ufoVersion == 2 and infoAttribute in fontInfoAttributesVersion2:
                copy = True
            elif self.ufoVersion == 3 and infoAttribute in fontInfoAttributesVersion3:
                copy = True
            if copy:
                value = getattr(sourceInfo, infoAttribute)
                setattr(targetInfo, infoAttribute, value)


