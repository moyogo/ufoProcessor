# ufoProcessor
Python package based on the **designSpaceDocument** (now [fontTools.designspaceLib](https://github.com/fonttools/fonttools/tree/master/Lib/fontTools/designspaceLib)) specifically to process and generate UFO files.

* Collect source materials
* Provide Mutators for specific glyphs, font info, kerning so that other tools can generate partial instances.
* Generate actual UFO instances in formats 2 and 3.
* Round geometry as requested
* Try to stay up to date with fontTools
* 

## Usage
The easiest way to use ufoProcessor is to call `build(designspacePath)`

* documentPath: path to the designspace file.
* outputUFOFormatVersion: integer, 2, 3. Format for generated UFOs. Note: can be different from source UFO format.
* roundGeometry: bool, if the geometry needs to be rounded to whole integers.
* processRules: bool, execute designspace rules as swaps.
* logger: optional logger object.

