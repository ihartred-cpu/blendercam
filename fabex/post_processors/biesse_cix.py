################################################################################
# biesse_cix.py
#
# Biesse CIX (CID3) post processor for Fabex
#
# Wraps standard ISO-style G-code as embedded ISO instructions inside a
# CIX (CID3 REL=5.0) container, matching the format Biesse's bSuite/bSolid
# accepts natively via the work list and CAD/CAM import.
#
# Confirmed against real bSuite behaviour:
#   - CID3 REL=5.0 header + MAINDATA panel block imports cleanly
#   - Standalone `BEGIN MACRO / NAME=ISO / PARAM,NAME=ISO,VALUE="..."`
#     blocks import as individual "ISO row" entries in the machining
#     operation timeline, carrying arbitrary embedded G-code text
#   - TP=<n> selects tool pocket n; TP0 releases the tool
#
# 3-axis only for now. 5-axis (G300 + B/C letters) intentionally left
# out until the indexed-rotation pipeline has been verified empirically.
################################################################################

from . import nc
from .iso import Creator as IsoCreator


class Creator(IsoCreator):
    def __init__(self):
        IsoCreator.__init__(self)

        # CIX/bSuite doesn't use ISO block numbers or a program-number
        # header the way generic ISO controls do - the CID3 container
        # itself carries program identity.
        self.output_block_numbers = False
        self.output_tool_definitions = False
        self.output_comment_before_tool_change = False

        # Buffer for building up one logical line at a time before
        # wrapping it as a CIX ISO-row macro.
        self._cix_buffer = ""
        self._wrote_header = False

    ############################################################################
    # CIX container structure

    def _panel_dims(self):
        """Panel size in mm, computed from the real bounding box of the
        operations being exported - NOT the machine's generic work-area
        setting (that's the machine's travel limits, not this job's stock
        size). Always converts Blender's native meters to mm, since CIX/
        bSuite is always mm regardless of the Blender scene's display unit.
        """
        try:
            import bpy

            ops = bpy.context.scene.cam_operations
            if len(ops) == 0:
                return 0.0, 0.0, 0.0
            min_x = min(o.min.x for o in ops)
            min_y = min(o.min.y for o in ops)
            min_z = min(o.min.z for o in ops)
            max_x = max(o.max.x for o in ops)
            max_y = max(o.max.y for o in ops)
            max_z = max(o.max.z for o in ops)
            x = (max_x - min_x) * 1000.0
            y = (max_y - min_y) * 1000.0
            z = (max_z - min_z) * 1000.0
        except Exception:
            x, y, z = 0.0, 0.0, 0.0
        return x, y, z

    def _write_cix_header(self):
        lpx, lpy, lpz = self._panel_dims()
        self.file.write("BEGIN ID CID3\n")
        self.file.write("  REL=5.0\n")
        self.file.write("END ID\n")
        self.file.write("BEGIN MAINDATA\n")
        self.file.write(f"        LPX={lpx:.4f}\n")
        self.file.write(f"        LPY={lpy:.4f}\n")
        self.file.write(f"        LPZ={lpz:.4f}\n")
        self.file.write('        MATERIAL="Fabex export"\n')
        self.file.write("        FCN=1.0000\n")
        self.file.write('        ORLST="1"\n')
        self.file.write("        SIMMETRY=0\n")
        self.file.write("        UNIQUE=1\n")
        self.file.write("END MAINDATA\n\n\n")
        self._wrote_header = True

    def _emit_iso_row(self, line):
        line = line.strip()
        if not line:
            return
        # CIX VALUE strings use doubled double-quotes to escape a literal quote
        escaped = line.replace('"', '""')
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=ISO\n")
        self.file.write(f'        PARAM,NAME=ISO,VALUE="{escaped}"\n')
        self.file.write("END MACRO\n\n")

    ############################################################################
    # Override the low-level write() so every accumulated line gets wrapped
    # as its own CIX ISO-row macro instead of written as raw text.

    def write(self, s):
        if self.output_disabled:
            return
        if not self._wrote_header:
            self._write_cix_header()
        self._cix_buffer += s
        while "\n" in self._cix_buffer:
            line, self._cix_buffer = self._cix_buffer.split("\n", 1)
            self._emit_iso_row(line)
        if "\n" in s:
            self.start_of_line = s[-1] == "\n"

    def writem(self, a):
        # nc.Creator.writem() bypasses write() entirely and goes straight
        # to self.file - iso.py's feed()/rapid() use this for every axis
        # word (X/Y/Z/A/B/C), so without this override every coordinate
        # would slip past the CIX-wrapping logic above.
        self.write("".join(a))

    ############################################################################
    # Program start/end - CID3's own header/MAINDATA replaces the usual
    # O-number program header, and we skip the generic M02 in favour of
    # just releasing the tool (TP0), matching the vendor sample pattern.

    def program_begin(self, id, name=""):
        # Header is written lazily on first real write() call, so nothing
        # extra is needed here - just record identity for reference.
        self.program_id = id
        self.program_name = name

    def PROGRAM_END(self):
        return "TP0"

    ############################################################################
    # Tool changes - Biesse's TP=<n> selects the tool pocket; spindle-on
    # is issued in the same instruction per the reference sample pattern.

    def tool_change(self, id):
        self.write(self.SPACE() + f"TP={id} M3")
        self.write("\n")
        self.t = id
        self.move_done_since_tool_change = False

    def TOOL(self):
        # Not used directly since tool_change() is overridden above,
        # but defined for completeness / in case any base-class path
        # still calls it.
        return "TP=%i"

    ############################################################################
    # Spindle - Biesse dialect uses plain M3/M5, no S-word peculiarities
    # beyond what the base ISO creator already does, so no override needed
    # here beyond what IsoCreator provides.


################################################################################

nc.creator = Creator()
