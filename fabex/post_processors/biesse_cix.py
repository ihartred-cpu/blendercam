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

import math

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
        self._write_geo_outline(lpx, lpy)
        self._wrote_header = True

    def _write_geo_outline(self, lpx, lpy):
        # Confirmed required via real bSuite testing (Fusion-side post,
        # same finding applies here): MAINDATA's LPX/LPY/LPZ only declares
        # bounding-box dimensions, not an actual geometry object. Without
        # a real GEO panel outline, operations attached to "the part" fail
        # with "empty geo (null) has not been imported". Simple rectangle
        # matching the panel corner-origin convention: (0,0) to (lpx,lpy).
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=GEO\n")
        self.file.write('        PARAM,NAME=ID,VALUE="PART1"\n')
        self.file.write("        PARAM,NAME=SIDE,VALUE=0\n")
        self.file.write('        PARAM,NAME=CRN,VALUE="1"\n')
        self.file.write("        PARAM,NAME=DP,VALUE=0\n")
        self.file.write("        PARAM,NAME=RTY,VALUE=rpNO\n")
        self.file.write('        PARAM,NAME=LAY,VALUE="GEO"\n')
        self.file.write("END MACRO\n\n")
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=START_POINT\n")
        self.file.write("        PARAM,NAME=ID,VALUE=1\n")
        self.file.write("        PARAM,NAME=X,VALUE=0\n")
        self.file.write("        PARAM,NAME=Y,VALUE=0\n")
        self.file.write("        PARAM,NAME=Z,VALUE=0\n")
        self.file.write("END MACRO\n\n")
        segments = [(2, lpx, 0.0), (3, lpx, lpy), (4, 0.0, lpy), (5, 0.0, 0.0)]
        for seg_id, xe, ye in segments:
            self.file.write("BEGIN MACRO\n")
            self.file.write("        NAME=LINE_EP\n")
            self.file.write(f"        PARAM,NAME=ID,VALUE={seg_id}\n")
            self.file.write(f"        PARAM,NAME=XE,VALUE={xe:.4f}\n")
            self.file.write(f"        PARAM,NAME=YE,VALUE={ye:.4f}\n")
            self.file.write("        PARAM,NAME=ZS,VALUE=0\n")
            self.file.write("        PARAM,NAME=ZE,VALUE=0\n")
            self.file.write("END MACRO\n\n")
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=ENDPATH\n")
        self.file.write("        PARAM,NAME=ID,VALUE=6\n")
        self.file.write("END MACRO\n\n")

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

    def write_wfl_macro(self, cangle_rad, tilt_rad, origin=(0.0, 0.0, 0.0)):
        # Native CIX WFL macro - NOT wrapped as an ISO row, since this is a
        # real macro type, not embedded G-code text.
        #
        # cangle_rad/tilt_rad come from Fabex's own rotation_to_2_axes()
        # (utilities/orient_utils.py), which prepare_indexed() calls with
        # the "CA" axis combination - NOT machine-specific to this unit.
        #
        # UPDATE: the machine's real kinematic chain has since been
        # definitively confirmed (from the actual TP1 system.xml file,
        # not simulation inference): C1 is OUTER, B1 is INNER, and B1's
        # real rotation-axis vector is (vx=-cos(40deg), vy=0, vz=sin(40deg))
        # - i.e. entirely in the X-Z plane, exact match to the known 40deg
        # Alpha cant. This is real, confirmed ground truth that did not
        # exist when this function was first written.
        #
        # The AZ/AR formula below has NOT been re-derived against this
        # confirmed geometry yet - it still uses the original placeholder
        # assumption. Given the chain order is now known to be the
        # opposite of what was originally assumed, this formula is
        # SUSPECT and should not be trusted for a real tilted cut without
        # re-deriving it from the confirmed vector above first. Flagging
        # rather than guessing a fix under time pressure - this needs
        # careful geometric work, not a quick patch.
        #
        # Origin defaults to (0,0,0) - a placeholder. Fusion's official
        # post computes this from the section's real work origin relative
        # to the workpiece bounds; Fabex's equivalent hasn't been wired up
        # yet. Flagging rather than guessing.
        wfl_id = getattr(self, "_wfl_id_counter", 6)
        self._wfl_id_counter = wfl_id + 1
        az = 90.0 - math.degrees(tilt_rad)
        ar = math.degrees(cangle_rad)
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=WFL\n")
        self.file.write(f"        PARAM,NAME=ID,VALUE={wfl_id}\n")
        self.file.write(f"        PARAM,NAME=X,VALUE={origin[0]:.4f}\n")
        self.file.write(f"        PARAM,NAME=Y,VALUE={origin[1]:.4f}\n")
        self.file.write(f"        PARAM,NAME=Z,VALUE={origin[2]:.4f}\n")
        self.file.write(f"        PARAM,NAME=AZ,VALUE={az:.3f}\n")
        self.file.write(f"        PARAM,NAME=AR,VALUE={ar:.3f}\n")
        self.file.write("        PARAM,NAME=L,VALUE=0\n")
        self.file.write("        PARAM,NAME=H,VALUE=0\n")
        self.file.write("        PARAM,NAME=AFL,VALUE=1\n")
        self.file.write("        PARAM,NAME=AFH,VALUE=1\n")
        self.file.write("        PARAM,NAME=UCS,VALUE=1\n")
        self.file.write("        PARAM,NAME=RV,VALUE=0\n")
        self.file.write("        PARAM,NAME=FRC,VALUE=1\n")
        self.file.write("END MACRO\n\n")
        return wfl_id

    def PROGRAM_END(self):
        return "TP0"

    def comment(self, text):
        # Suppress Fabex's identification/info comments (version banner,
        # rapid-feed note, etc.) entirely - they add no functional value
        # to the actual program, and the long version-banner comment is
        # the suspected cause of a "closed parenthesis missing" parse
        # error from bSuite - possibly a VALUE string length limit
        # silently truncating it. Simplest safe fix: don't emit them.
        pass

    def program_end(self):
        # iso.py's program_end() runs a post-write pass (number_file()) that
        # reopens the finished file and prepends N<number> to every single
        # line - including CID3 keywords like "BEGIN MACRO" and "END MACRO".
        # That's fundamentally incompatible with the CIX format, regardless
        # of the output_block_numbers setting (which gcode_export.py
        # re-applies from the machine's Blender settings after __init__
        # anyway, so setting it False there doesn't stick). Override the
        # full method here so that pass never runs for this post.
        if self.z_for_g53 is not None:
            self.write(
                self.SPACE()
                + self.MACHINE_COORDINATES()
                + self.SPACE()
                + "Z"
                + self.fmt.string(self.z_for_g53)
                + "\n"
            )
        self.write(self.SPACE() + self.PROGRAM_END() + "\n")

        if self.temp_file_to_append_on_close is not None:
            f_in = open(self.temp_file_to_append_on_close, "r")
            while True:
                line = f_in.readline()
                if len(line) == 0:
                    break
                self.write(line)
            f_in.close()

        self.file_close()

    ############################################################################
    # Tool changes - a bare "TP=<n> M3" ISO-embedded string does NOT
    # properly register tool selection in bSuite's internal state
    # (confirmed empirically: produces "no spindles selected" even with
    # clean, valid syntax). Cross-validated two ways: the Fabex thread's
    # own v10 finding (a real ROUT operation with a real tool was needed
    # before G300 moves would work at all) and a real reference file
    # posted from Fusion's own official stock Biesse CIX post, which
    # activates tools via a native ROUT/ROUTG macro's TNM=<tool name>
    # parameter. This mirrors the same fix already validated on the
    # Fusion-side post (biesse_cix_3axis.cps).
    #
    # Minimal field set (not the full ~55-parameter ROUTG, which requires
    # an external GID-referenced GEO macro and produced "geometry not
    # found" when tested without one) - CV's own proven working export
    # only ever sets tool name and diameter, so those two are the fields
    # trusted here; TTP intentionally omitted for the same reason.
    #
    # DP is intentionally a small positive value, NOT 0 - confirmed via
    # real testing that DP=0 does not mean "no-op cut" and instead
    # produced an uncontrolled deep plunge. Real vendor samples use a
    # genuine positive depth (e.g. DP=5).

    def tool_change(self, id):
        op = getattr(self, "current_operation", None)
        diameter_mm = (op.cutter_diameter * 1000.0) if op is not None else 12.7
        tool_name = (op.cutter_description if op is not None else "") or f"T{id}"

        rout_id = getattr(self, "_rout_id_counter", 1001)
        self._rout_id_counter = rout_id + 1

        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=ROUT\n")
        self.file.write(f'        PARAM,NAME=ID,VALUE="P{rout_id}"\n')
        self.file.write("        PARAM,NAME=SIDE,VALUE=0\n")
        self.file.write('        PARAM,NAME=CRN,VALUE="1"\n')
        self.file.write("        PARAM,NAME=Z,VALUE=0\n")
        self.file.write("        PARAM,NAME=DP,VALUE=1\n")
        self.file.write("        PARAM,NAME=OPT,VALUE=NO\n")
        self.file.write(f"        PARAM,NAME=DIA,VALUE={diameter_mm:.3f}\n")
        self.file.write("        PARAM,NAME=RTY,VALUE=rpNO\n")
        self.file.write("        PARAM,NAME=XRC,VALUE=16\n")
        self.file.write("        PARAM,NAME=YRC,VALUE=16\n")
        self.file.write("        PARAM,NAME=DX,VALUE=32\n")
        self.file.write("        PARAM,NAME=DY,VALUE=32\n")
        self.file.write("        PARAM,NAME=R,VALUE=0\n")
        self.file.write("        PARAM,NAME=A,VALUE=0\n")
        self.file.write("        PARAM,NAME=THR,VALUE=NO\n")
        self.file.write("        PARAM,NAME=RV,VALUE=NO\n")
        self.file.write("        PARAM,NAME=CRC,VALUE=0\n")
        self.file.write(f'        PARAM,NAME=TNM,VALUE="{tool_name}"\n')
        self.file.write("END MACRO\n\n")
        self.file.write("BEGIN MACRO\n")
        self.file.write("        NAME=ENDPATH\n")
        self.file.write("END MACRO\n\n")

        self.t = id
        self.move_done_since_tool_change = False

    def write_noprk(self, start, distance=400):
        # Native NOPRK "magic comment" directive - confirmed via real
        # NOPRK.vbs source, not just documentation (which contradicted
        # itself on this point). start=True emits ";NOPRKSTART DST=<n>"
        # (suppresses the normal park move between operations closer
        # together than `distance`); start=False emits ";NOPRKEND"
        # (restores normal parking). Default distance 400 matches the
        # real hardcoded fallback in Biesse's own wrapper functions.
        # NOT wired in automatically - call explicitly around a run of
        # closely-spaced operations if the default parking behaviour
        # produces unwanted full-park retracts.
        if start:
            self._emit_iso_row(f";NOPRKSTART DST={distance}")
        else:
            self._emit_iso_row(";NOPRKEND")

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
