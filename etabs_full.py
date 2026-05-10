import comtypes.client
import pandas as pd
import numpy as np
import random
import time
import os
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime

# ============================================================================
# USER CONFIGURATION (all knobs in one place)
# ============================================================================
CONFIG = {
    # Mode: "C" = beams+columns (only option currently)
    "MODE": "C",

    # Optimization parameters
    "STEP_MM": 25,                 # Dimension step (mm)
    "GENS": 5,                      # Number of generations
    "POP": 5,                       # Population size
    "MM_MIN": 250,                   # Minimum dimension (mm)
    "MM_MAX": 1000,                   # Maximum dimension (mm)
    "ASPECT_MAX": 3.0,                # Max h/b or b/h ratio
    "BASE_EDB": "Trial.EDB",          # ETABS model file
    "WORK_ON_COPY": True,             # Run optimization on a timestamped model copy
    "RUNS_DIR": "runs",               # Folder that stores working model copies


    # Penalty factors (must dominate any feasible volume cost)
    "PEN_FAIL": 1e15,                 # Per failed member
    "PEN_DRIFT": 1e14,                 # Penalty if drift exceeds limit
    "COST_PER_M3": 20000,               # Base concrete cost per m^3 (C20)

    # GA parameters
    "PARENTS": 5,                      # Number of parents for mating pool
    "MUT_RATE": 0.4,                   # Mutation probability
    "TOUR_K": 3,                        # Tournament size (not currently used)
    "ELITE_COUNT": 1,                    # Number of elites to keep

    # Drift check (requires Story Drifts table)
    "ENABLE_DRIFT_CHECK": False,
    "ALLOW_DRIFT": 0.02,                 # H/50

    # Material grids
    "CONC_GRADES": [20, 25, 30, 35, 40, 45, 50],   # MPa
    "STEEL_GRADES": [270, 420, 500, 550],           # MPa

    # Cost multipliers
    "CONC_COST_FACTORS": {20:1.0, 25:1.1, 30:1.25, 35:1.4, 40:1.6, 45:1.85, 50:2.1},
    "STEEL_COST_FACTORS": {270:1.0, 420:1.2, 500:1.35, 550:1.5},
    "REBAR_COST_PER_KG": 250,
    "STEEL_DENSITY_KG_M3": 7850,

    # Soft max dimension penalty
    "SOFT_MAX_DIM": 850,                # Dimensions above this incur penalty
    "PEN_SOFT_MAX": 50000,               # Base penalty for exceeding soft max

    # ETABS timeouts
    "DESIGN_TIMEOUT": 45,                # Seconds to wait for design completion
    "POLL_INTERVAL": 1.5,                # Seconds between polls
}

# ============================================================================
# CONSTANTS (ETABS API)
# ============================================================================
UNITS_N_MM = 9                          # N, mm, C (Celsius)
MAT_CONCRETE = 2
MAT_REBAR = 6

# ============================================================================
# ETABS CLIENT (improved)
# ============================================================================
class EtabsClient:
    """Encapsulates ETABS API interactions with caching and robust error handling."""

    def __init__(self, config):
        self.config = config
        self.SapModel = None
        self.EtabsObject = None
        self.beams = []
        self.columns = []
        self.len_beams = 0.0
        self.len_cols = 0.0
        self.design_code = None
        self.stop_requested = False
        self.model_path = None
        self.beam_work_section = None
        self.column_work_section = None

        # Caches
        self._material_cache = {}          # material name -> success
        self._section_cache = {}            # (name, mat, h, w) -> success flag
        self._frame_length_cache = {}       # frame name -> length (m)
        self._available_tables_cache = None
        self._design_code_api = None
        self._section_rebar_templates = {"beam": None, "column": None}

    def connect(self):
        """Connect to an existing ETABS instance or start a new one."""
        self.SapModel = None
        self.EtabsObject = None

        try:
            candidate = comtypes.client.GetActiveObject("CSI.ETABS.API.ETABSObject")
            candidate_model = candidate.SapModel
            candidate_model.SetPresentUnits(UNITS_N_MM)
            self.EtabsObject = candidate
            self.SapModel = candidate_model
            print("[ETABS] Attached to active instance.")
            print("[ETABS] Units set to N-mm.")
            return
        except Exception:
            print("[ETABS] No active instance found. Starting new instance...")
            try:
                helper = comtypes.client.CreateObject('ETABSv1.Helper')
                helper = helper.QueryInterface(comtypes.gen.ETABSv1.cHelper)
                self.EtabsObject = helper.CreateObjectProgID("CSI.ETABS.API.ETABSObject")
                self.EtabsObject.ApplicationStart()
                print("[ETABS] Started new instance.")
            except Exception as e:
                print(f"[ETABS] Failed to start ETABS: {e}")
                raise

        last_error = None
        for _ in range(10):
            try:
                self.SapModel = self.EtabsObject.SapModel
                self.SapModel.SetPresentUnits(UNITS_N_MM)
                print("[ETABS] Units set to N-mm.")
                return
            except Exception as e:
                last_error = e
                time.sleep(1)

        raise last_error

    def _resolve_model_path(self, model_path):
        """Resolve the model path relative to the working directory or this script."""
        path = Path(model_path)
        if path.is_absolute():
            return path

        cwd_candidate = Path.cwd() / path
        if cwd_candidate.exists():
            return cwd_candidate.resolve()

        script_candidate = Path(__file__).resolve().parent / path
        return script_candidate.resolve()

    def prepare_model_copy(self, model_path):
        """Copy the model and common companion files into a timestamped run folder."""
        source = self._resolve_model_path(model_path)
        if not source.exists():
            raise FileNotFoundError(f"Model file not found: {source}")

        if not self.config.get("WORK_ON_COPY", True):
            return str(source)

        runs_dir = self._resolve_model_path(self.config.get("RUNS_DIR", "runs"))
        runs_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = runs_dir / f"{source.stem}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)

        source_dir = source.parent
        companion_suffixes = {source.suffix.lower(), ".ebk", ".$et"}
        for candidate in source_dir.iterdir():
            if candidate.is_file() and candidate.stem == source.stem and candidate.suffix.lower() in companion_suffixes:
                shutil.copy2(candidate, run_dir / candidate.name)

        return str((run_dir / source.name).resolve())

    def _safe_design_code(self):
        """Return a normalized concrete design code string."""
        try:
            res = self.SapModel.DesignConcrete.GetCode()
            if isinstance(res, (list, tuple)) and res:
                for item in res:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
        except Exception:
            pass
        return "ACI 318-19"

    def _normalize_code_token(self, text):
        return re.sub(r"[^a-z0-9]", "", str(text).lower())

    def _get_design_code_api(self):
        """Return the code-specific design API when ETABS exposes one."""
        if self._design_code_api is not None:
            return self._design_code_api

        code_token = self._normalize_code_token(self.design_code)
        concrete_design = self.SapModel.DesignConcrete

        for attr in dir(concrete_design):
            if attr.startswith("_"):
                continue
            if self._normalize_code_token(attr) == code_token:
                self._design_code_api = getattr(concrete_design, attr)
                return self._design_code_api

        self._design_code_api = False
        return None

    def _normalize_table_result(self, result):
        """Coerce ETABS table results from different API versions into one shape."""
        if not isinstance(result, (list, tuple)) or len(result) < 4:
            return None

        ret = result[-1]
        if ret != 0:
            return None

        fields = None
        num_rows = None
        data = None

        list_items = [list(item) for item in result[:-1] if isinstance(item, (list, tuple)) and item]
        int_items = [item for item in result[:-1] if isinstance(item, int) and item >= 0]

        string_lists = [item for item in list_items if all(isinstance(v, str) or v is None for v in item)]
        for i, candidate_fields in enumerate(string_lists):
            if not candidate_fields:
                continue
            for candidate_data in string_lists[i + 1:]:
                if len(candidate_data) >= len(candidate_fields) and len(candidate_data) % len(candidate_fields) == 0:
                    fields = candidate_fields
                    data = candidate_data
                    break
            if fields is not None and data is not None:
                break

        if data is None:
            for item in list_items:
                if item is fields:
                    continue
                if not all(isinstance(v, str) for v in item):
                    data = item
                    break

        if fields is None or data is None:
            return None

        width = len(fields)
        if width == 0:
            return None

        matching_rows = [value for value in int_items if value * width == len(data)]
        if matching_rows:
            num_rows = matching_rows[0]
        else:
            num_rows = len(data) // width

        return {
            "fields": fields,
            "num_rows": num_rows,
            "data": data,
            "width": width,
        }

    def _coerce_float(self, value):
        text = str(value).replace(",", "").replace(">", "").replace("<", "").strip()
        if not text:
            raise ValueError("empty numeric field")
        return float(text)

    def _coerce_float_list(self, values):
        """Convert an ETABS result array to numeric values, skipping blanks."""
        result = []
        for value in values:
            try:
                result.append(self._coerce_float(value))
            except Exception:
                pass
        return result

    def _list_available_tables(self):
        """Best-effort lookup for ETABS database table names."""
        if self._available_tables_cache is not None:
            return self._available_tables_cache

        db = self.SapModel.DatabaseTables
        candidates = (
            "GetAvailableTables",
            "GetAllTables",
            "GetTableNames",
        )
        for method_name in candidates:
            try:
                method = getattr(db, method_name)
                result = method()
                table_names = []
                if isinstance(result, (list, tuple)):
                    for item in result:
                        if isinstance(item, (list, tuple)):
                            table_names.extend([v for v in item if isinstance(v, str)])
                if table_names:
                    self._available_tables_cache = table_names
                    return table_names
            except Exception:
                continue

        self._available_tables_cache = []
        return self._available_tables_cache

    def _resolve_table_name(self, *candidates):
        """Find the first matching ETABS database table name."""
        cleaned = [c for c in candidates if c]
        for table_name in cleaned:
            try:
                result = self.SapModel.DatabaseTables.GetTableForDisplayArray(
                    table_name, [], "", 0, [], 0, []
                )
                if self._normalize_table_result(result):
                    return table_name
            except Exception:
                pass

        available = self._list_available_tables()
        normalized_available = {self._normalize_code_token(name): name for name in available}
        for candidate in cleaned:
            token = self._normalize_code_token(candidate)
            if token in normalized_available:
                return normalized_available[token]

        return cleaned[0] if cleaned else None

    def _design_table_candidates(self, base_name):
        code = (self.design_code or "").strip()
        code_no_spaces = code.replace(" ", "")
        return [
            f"{base_name} - {code}" if code else None,
            f"{base_name}-{code}" if code else None,
            f"{base_name} - {code_no_spaces}" if code_no_spaces else None,
            base_name,
        ]

    def open_model(self, model_path):
        """Open an existing ETABS model."""
        full_path = str(self._resolve_model_path(model_path))
        self.model_path = full_path
        if not os.path.exists(full_path):
            print(f"[ETABS] Model file not found: {full_path}")
            return False
        print(f"[ETABS] Opening model: {full_path}")
        ret = self.SapModel.File.OpenFile(full_path)
        if ret != 0:
            print(f"[ETABS] Error opening file {full_path}. Ret: {ret}")
            return False

        time.sleep(2)  # Let model load
        self.SapModel.SetPresentUnits(UNITS_N_MM)

        # Get design code (for table names)
        self.design_code = self._safe_design_code()
        print(f"[ETABS] Detected design code: {self.design_code}")

        return True

    def scan_frames(self):
        """Categorize frames into beams and columns based on design orientation."""
        res = self.SapModel.FrameObj.GetNameList()
        if len(res) == 3:
            number, names, ret = res
        else:
            print(f"[ERROR] GetNameList unexpected return: {res}")
            return

        if ret != 0 or number == 0:
            print(f"[ETABS] No frames found! Ret={ret}, Num={number}")
            return

        self.beams = []
        self.columns = []

        for name in names:
            # Design orientation: 1 = column, 2 = beam
            orientation = self.SapModel.FrameObj.GetDesignOrientation(name)[0]
            if orientation == 1:
                self.columns.append(name)
            elif orientation == 2:
                self.beams.append(name)

        print(f"[ETABS] Found {len(self.beams)} beams and {len(self.columns)} columns.")
        try:
            if self.beams:
                self.beam_work_section = self.SapModel.FrameObj.GetSection(self.beams[0])[0]
            if self.columns:
                self.column_work_section = self.SapModel.FrameObj.GetSection(self.columns[0])[0]
        except Exception:
            pass
        self.capture_rebar_templates()

    def capture_rebar_templates(self):
        """Snapshot the current beam/column rebar section settings for reuse on new sections."""
        if self.beams and self._section_rebar_templates["beam"] is None:
            try:
                beam_sec = self.SapModel.FrameObj.GetSection(self.beams[0])[0]
                beam_rebar = self.SapModel.PropFrame.GetRebarBeam(beam_sec)
                if beam_rebar and beam_rebar[-1] == 0:
                    self._section_rebar_templates["beam"] = list(beam_rebar[:-1])
            except Exception:
                pass

        if self.columns and self._section_rebar_templates["column"] is None:
            try:
                col_sec = self.SapModel.FrameObj.GetSection(self.columns[0])[0]
                col_rebar = self.SapModel.PropFrame.GetRebarColumn(col_sec)
                if col_rebar and col_rebar[-1] == 0:
                    self._section_rebar_templates["column"] = list(col_rebar[:-1])
            except Exception:
                pass

    def get_frame_length(self, frame_name):
        """Return length of a frame in meters (cached)."""
        if frame_name in self._frame_length_cache:
            return self._frame_length_cache[frame_name]

        try:
            res = self.SapModel.FrameObj.GetPoints(frame_name)
            p1, p2 = res[0], res[1]
            x1, y1, z1 = self.SapModel.PointObj.GetCoordCartesian(p1)[:3]
            x2, y2, z2 = self.SapModel.PointObj.GetCoordCartesian(p2)[:3]
            length_mm = ((x2-x1)**2 + (y2-y1)**2 + (z2-z1)**2)**0.5
            length_m = length_mm / 1000.0
            self._frame_length_cache[frame_name] = length_m
            return length_m
        except Exception as e:
            print(f"[WARNING] Could not get length for {frame_name}: {e}")
            return 0.0

    def calculate_total_lengths(self):
        """Sum lengths of all beams and columns."""
        self.len_beams = sum(self.get_frame_length(b) for b in self.beams)
        self.len_cols = sum(self.get_frame_length(c) for c in self.columns)
        print(f"[ETABS] Total beam length: {self.len_beams:.2f} m, column length: {self.len_cols:.2f} m")

    def define_concrete(self, fc_mpa):
        """Define or retrieve a concrete material with given strength."""
        grade = int(round(fc_mpa))
        name = f"C{grade}"
        if name in self._material_cache:
            return name

        # eMatType_Concrete = 2
        ret = self.SapModel.PropMaterial.SetMaterial(name, MAT_CONCRETE)
        if ret != 0:
            # Material may already exist? Try to get it.
            try:
                self.SapModel.PropMaterial.GetMaterial(name, MAT_CONCRETE)
                self._material_cache[name] = name
                return name
            except:
                raise Exception(f"Could not create or get material {name}")

        # ACI 318-19: Ec = 4700 * sqrt(fc') in MPa
        ec = 4700 * (fc_mpa ** 0.5)
        v = 0.2
        a = 9.9e-6
        ret = self.SapModel.PropMaterial.SetMPIsotropic(name, ec, v, a)
        # SetOConcrete(Name, Fc, IsLightweight, FcsFactor, SSType, SSHysType, StrainAtFc, StrainUltimate)
        ret = self.SapModel.PropMaterial.SetOConcrete(name, fc_mpa, False, 0.0, 1, 4, 0.002, 0.003)
        self._material_cache[name] = name
        return name

    def define_rebar(self, fy_mpa):
        """Define or retrieve a rebar material with given yield strength."""
        grade = int(round(fy_mpa))
        name = f"S{grade}"
        if name in self._material_cache:
            return name

        ret = self.SapModel.PropMaterial.SetMaterial(name, MAT_REBAR)
        if ret != 0:
            try:
                self.SapModel.PropMaterial.GetMaterial(name, MAT_REBAR)
                self._material_cache[name] = name
                return name
            except:
                raise Exception(f"Could not create or get material {name}")

        es = 200000.0
        v = 0.3
        a = 1.17e-5
        ret = self.SapModel.PropMaterial.SetMPIsotropic(name, es, v, a)
        fu = fy_mpa * 1.15
        ret = self.SapModel.PropMaterial.SetORebar(name, fy_mpa, fu, fy_mpa, fu, 1, 1, 0.01, 0.1, False)
        self._material_cache[name] = name
        return name

    def define_rect_section(self, name, mat_name, depth, width):
        """Define or update a rectangular frame section using SetRectangle only."""
        key = (name, mat_name, depth, width)
        if key in self._section_cache:
            return self._section_cache[key]

        # SetRectangle creates a new property if it doesn't exist,
        # or updates an existing one. Return code 0 means success.
        ret = self.SapModel.PropFrame.SetRectangle(name, mat_name, depth, width)
        success = (ret == 0)
        self._section_cache[key] = success
        return success

    def _get_section_dims(self, section_name):
        """Return rectangular section depth and width in mm, or (None, None)."""
        try:
            res = self.SapModel.PropFrame.GetRectangle(section_name)
            if len(res) >= 4:
                return float(res[2]), float(res[3])
        except Exception:
            pass
        return None, None

    def _column_rebar_trials(self, steel_mat_name, section_name):
        """Yield progressively safer column rebar layouts for the target section."""
        template = self._section_rebar_templates.get("column")
        depth, width = self._get_section_dims(section_name)
        min_dim = min([v for v in (depth, width) if v is not None], default=300.0)
        cover = 25.0 if min_dim <= 300 else 38.1

        trials = []
        if template:
            base = list(template)
            base[0] = steel_mat_name
            base[1] = steel_mat_name
            trials.append(tuple(base[2:]))

            # Retry with fewer bars while keeping the model's native bar labels.
            for r3, r2, n2, n3 in [(3, 4, 2, 2), (3, 3, 2, 2)]:
                reduced = list(base)
                if len(reduced) >= 14:
                    reduced[4] = cover
                    reduced[6] = r3
                    reduced[7] = r2
                    reduced[11] = n2
                    reduced[12] = n3
                trials.append(tuple(reduced[2:]))

        # Generic safe fallbacks for small columns.
        generic_layouts = [
            (1, 0, cover, 0, 3, 3, "#8", "#4", 152.4, 2, 2, True),
            (1, 0, cover, 0, 3, 3, "#6", "#3", 100.0, 2, 2, True),
            (1, 0, cover, 0, 3, 3, "#5", "#3", 100.0, 2, 2, True),
        ]
        trials.extend(generic_layouts)

        seen = set()
        for trial in trials:
            if trial not in seen:
                seen.add(trial)
                yield trial

    def assign_section_to_frames(self, frames, section_name):
        """Assign a section to a list of frames."""
        for frame in frames:
            self.SapModel.FrameObj.SetSection(frame, section_name)

    def _get_section_names(self):
        """Return all frame section property names."""
        try:
            result = self.SapModel.PropFrame.GetNameList()
            if len(result) >= 3 and result[-1] == 0:
                return set(result[1])
        except Exception:
            pass
        return set()

    def _build_final_section_name(self, prefix, dims, conc_grade, steel_grade):
        """Build a readable ETABS section name for the final selected design."""
        base = f"{prefix}_{dims[0]}x{dims[1]}_C{conc_grade}_S{steel_grade}"
        return self._make_unique_section_name(base)

    def _make_unique_section_name(self, base):
        """Return a unique frame section property name based on the requested base."""
        existing = self._get_section_names()
        if base not in existing:
            return base

        suffix = 1
        while f"{base}_{suffix}" in existing:
            suffix += 1
        return f"{base}_{suffix}"

    def rename_section(self, old_name, new_name):
        """Rename a frame section property and return the resolved name."""
        if not old_name:
            return old_name
        if old_name == new_name:
            return old_name

        if self.SapModel.GetModelIsLocked():
            self.SapModel.SetModelIsLocked(False)

        resolved = new_name
        resolved = self._make_unique_section_name(resolved)

        try:
            ret = self.SapModel.PropFrame.ChangeName(old_name, resolved)
            if ret == 0:
                if self.beam_work_section == old_name:
                    self.beam_work_section = resolved
                if self.column_work_section == old_name:
                    self.column_work_section = resolved
                return resolved
        except Exception:
            pass
        return old_name

    def save_model(self):
        """Persist the current working model to disk."""
        try:
            ret = self.SapModel.File.Save()
            return ret == 0
        except Exception:
            return False

    def create_final_section(self, prefix, dims, conc_grade, steel_grade, is_beam):
        """Create a real final ETABS section property that matches the selected design."""
        if self.SapModel.GetModelIsLocked():
            self.SapModel.SetModelIsLocked(False)

        conc_mat = self.define_concrete(conc_grade)
        steel_mat = self.define_rebar(steel_grade)
        final_name = self._build_final_section_name(prefix, dims, conc_grade, steel_grade)

        ok = self.define_rect_section(final_name, conc_mat, dims[0], dims[1])
        if not ok:
            raise RuntimeError(f"Could not define final section {final_name}.")

        if not self.apply_section_rebar(final_name, steel_mat, is_beam=is_beam):
            raise RuntimeError(f"Could not assign rebar to final section {final_name}.")

        return final_name

    def apply_section_rebar(self, section_name, steel_mat_name, is_beam):
        """Apply cached beam/column rebar settings to a section using the active steel material."""
        if self.SapModel.GetModelIsLocked():
            self.SapModel.SetModelIsLocked(False)

        kind = "beam" if is_beam else "column"
        template = self._section_rebar_templates.get(kind)
        if not template:
            return False

        try:
            if is_beam:
                ret = self.SapModel.PropFrame.SetRebarBeam(
                    section_name,
                    steel_mat_name,
                    steel_mat_name,
                    *template[2:]
                )
                return ret == 0
            else:
                for trial in self._column_rebar_trials(steel_mat_name, section_name):
                    ret = self.SapModel.PropFrame.SetRebarColumn(
                        section_name,
                        steel_mat_name,
                        steel_mat_name,
                        *trial
                    )
                    if ret == 0:
                        return True
                return False
        except Exception:
            return False

    def run_analysis_design(self):
        """Run analysis and start design, wait for completion."""
        if self.SapModel.GetModelIsLocked():
            self.SapModel.SetModelIsLocked(False)

        ret = self.SapModel.Analyze.RunAnalysis()
        if ret != 0:
            print(f"[ETABS] Analysis failed. Ret={ret}")
            return False

        # Ensure design load combinations are assigned
        try:
            res_combos = self.SapModel.DesignConcrete.GetComboStrength()
            if res_combos[-1] != 0 or res_combos[0] == 0:
                # No combos assigned, so select likely strength combos with a wide heuristic.
                res_all = self.SapModel.RespCombo.GetNameList()
                if res_all[-1] == 0:
                    combo_names = list(res_all[1]) if len(res_all) > 1 else []
                    keywords = ("DCON", "DESIGN", "ULS", "STRENGTH", "FACTORED")
                    strength_combos = [n for n in combo_names if any(k in n.upper() for k in keywords)]
                    if not strength_combos:
                        strength_combos = combo_names
                    if strength_combos:
                        print(f"[ETABS] Assigning {len(strength_combos)} design combos.")
                        for c in strength_combos:
                            self.SapModel.DesignConcrete.SetComboStrength(c, True)
                    else:
                        print("[WARNING] No response combinations found to assign.")
        except Exception:
            pass

        try:
            self.SapModel.DesignConcrete.DeleteResults()
        except Exception:
            pass


        # Set sway/frame overwrite when the active design code exposes it.
        try:
            code_api = self._get_design_code_api()
            if code_api and hasattr(code_api, "SetOverwrite"):
                for frame in self.beams + self.columns:
                    code_api.SetOverwrite(frame, 1, 2.0, False)
        except Exception:
            pass

        self.SapModel.DesignConcrete.StartDesign()


        # Wait for design to finish
        timeout = self.config["DESIGN_TIMEOUT"]
        poll = self.config["POLL_INTERVAL"]
        elapsed = 0.0
        design_done = False
        while elapsed < timeout:
            time.sleep(poll)
            elapsed += poll
            try:
                if self.SapModel.DesignConcrete.GetResultsAvailable():
                    design_done = True
                    break
            except Exception:
                # Method not available; fall back to waiting full timeout
                pass

        if not design_done:
            print("[ETABS] Design poll timed out; giving extra buffer.")
            time.sleep(2)   # Final buffer
        return True

    def verify_design(self):
        """
        Returns (num_failed, total_rebar_kg).
        Uses:
          - DesignConcrete.VerifyPassed() for high-level fail count.
          - Design summary tables for per-member verification and rebar area.
        """
        verify_failed = 0
        summary_failed = 0
        total_rebar_kg = 0.0

        # Layer 1: VerifyPassed (counts failed members)
        try:
            res = self.SapModel.DesignConcrete.VerifyPassed()
            # Returns (NumFailed, ret) or (NumFailed, NumWarning, ret)
            if res[-1] == 0:
                verify_failed = int(res[0]) if res[0] else 0
        except Exception:
            pass


        # Layer 2: Scan design summary tables
        beam_table = self._resolve_table_name(*self._design_table_candidates("Concrete Beam Design Summary"))
        col_table = self._resolve_table_name(*self._design_table_candidates("Concrete Column Design Summary"))

        # Also scan reinforcement data for accurate rebar area
        beam_rebar_table = self._resolve_table_name(*self._design_table_candidates("Concrete Beam Reinforcement Data"))
        col_rebar_table = self._resolve_table_name(*self._design_table_candidates("Concrete Column Reinforcement Data"))

        # Process beams
        fails_beam, _ = self._parse_design_summary(beam_table, self.beams, is_beam=True)
        summary_failed += fails_beam
        # Get rebar from reinforcement data table (more accurate)
        rebar_beam_kg = self._get_rebar_weight(beam_rebar_table, self.beams, is_beam=True)
        total_rebar_kg += rebar_beam_kg

        # Process columns
        fails_col, _ = self._parse_design_summary(col_table, self.columns, is_beam=False)
        summary_failed += fails_col
        rebar_col_kg = self._get_rebar_weight(col_rebar_table, self.columns, is_beam=False)
        total_rebar_kg += rebar_col_kg

        if total_rebar_kg <= 0:
            raise RuntimeError("No rebar quantity could be extracted from ETABS design results.")

        # Both APIs inspect the same design results, so take the safer non-duplicated count.
        return max(verify_failed, summary_failed), total_rebar_kg


    def _parse_design_summary(self, table_name, frame_list, is_beam=True):
        """
        Parse design summary table to count failures.
        Returns (fail_count, total_rebar_area_from_summary) ; rebar from summary is less accurate.
        """
        if not frame_list:
            return 0, 0.0

        try:
            result = self.SapModel.DatabaseTables.GetTableForDisplayArray(
                table_name, [], "", 0, [], 0, [])
            table = self._normalize_table_result(result)
            if not table:
                return 0, 0.0
            fields = table["fields"]
            num_rows = table["num_rows"]
            data = table["data"]

            fields_lower = [f.lower() for f in fields]
            # Status columns
            status_idx = [i for i, f in enumerate(fields_lower) if any(k in f for k in ["status", "error", "check"])]
            ratio_idx = [i for i, f in enumerate(fields_lower) if any(k in f for k in ["ratio", "pmm", "d/c", "utilization"])]

            # Rebar area columns (sometimes present)
            rebar_idx = [i for i, f in enumerate(fields_lower) if "reinf area" in f or "as" in f]

            width = table["width"]
            fails = 0
            rebar_area_sum = 0.0

            for r in range(num_rows):
                row = data[r*width : (r+1)*width]
                # Check status
                for idx in status_idx:
                    if idx < len(row) and any(k in str(row[idx]).lower() for k in ["o/s", "fail", "over", "ng"]):
                        fails += 1
                        break
                # Check ratios
                for idx in ratio_idx:
                    if idx < len(row):
                        try:
                            val = self._coerce_float(row[idx])
                            if val > 1.0:
                                fails += 1
                                break
                        except:
                            pass
                # Sum rebar area (if available)
                for idx in rebar_idx:
                    if idx < len(row):
                        try:
                            rebar_area_sum += self._coerce_float(row[idx])
                        except:
                            pass

            return fails, rebar_area_sum
        except Exception as e:
            print(f"[WARNING] Failed to parse table {table_name}: {e}")
            return 0, 0.0

    def _get_rebar_weight(self, table_name, frame_list, is_beam=True):
        """Extract total rebar weight (kg) from reinforcement data table or summary API."""
        if not frame_list:
            return 0.0

        total_weight = 0.0
        density = self.config["STEEL_DENSITY_KG_M3"] / 1e9  # kg/mm^3

        try:
            # Attempt 1: Database Table
            result = self.SapModel.DatabaseTables.GetTableForDisplayArray(table_name, [], "", 0, [], 0, [])
            table = self._normalize_table_result(result)
            if table and table["num_rows"] > 0:
                fields = table["fields"]
                num_rows = table["num_rows"]
                data = table["data"]
                fields_lower = [f.lower() for f in fields]
                
                area_idx = next((i for i, f in enumerate(fields_lower) if "total rebar area" in f or "area of reinforcement" in f), None)
                weight_idx = next((i for i, f in enumerate(fields_lower) if "rebar weight" in f or "total weight" in f), None)

                if weight_idx is not None:
                    for r in range(num_rows):
                        row = data[r*len(fields) : (r+1)*len(fields)]
                        try: total_weight += self._coerce_float(row[weight_idx])
                        except: pass
                    return total_weight

                if area_idx is not None:
                    for r in range(num_rows):
                        row = data[r*len(fields) : (r+1)*len(fields)]
                        try:
                            area_mm2 = self._coerce_float(row[area_idx])
                            frame_name = str(row[0]).strip()
                            length_mm = self.get_frame_length(frame_name) * 1000
                            total_weight += area_mm2 * length_mm * density
                        except: pass
                    return total_weight

        except Exception:
            pass

        # Attempt 2: Fallback to GetSummaryResults API (if table failed or returned 0)
        if total_weight == 0:
            for frame in frame_list:
                try:
                    if is_beam:
                        res = self.SapModel.DesignConcrete.GetSummaryResultsBeam(frame)
                        if res[-1] == 0 and res[0] > 0:
                            # For the legacy beam summary API, the matched bottom steel areas
                            # are returned alongside locations; use bottom steel only.
                            locations = self._coerce_float_list(res[2] if len(res) > 2 else [])
                            bottom_areas = self._coerce_float_list(res[6] if len(res) > 6 else [])
                            if locations and bottom_areas and len(locations) == len(bottom_areas):
                                max_area = max(bottom_areas)
                                length_mm = self.get_frame_length(frame) * 1000
                                total_weight += max_area * length_mm * density
                            elif bottom_areas:
                                max_area = max(bottom_areas)
                                length_mm = self.get_frame_length(frame) * 1000
                                total_weight += max_area * length_mm * density
                    else:
                        # GetSummaryResultsColumn: Index 5 usually contains longitudinal rebar area
                        res = self.SapModel.DesignConcrete.GetSummaryResultsColumn(frame)
                        if res[-1] == 0 and res[0] > 0:
                            areas = res[5] if len(res) > 5 else []
                            if areas:
                                max_area = max(areas)
                                length_mm = self.get_frame_length(frame) * 1000
                                total_weight += max_area * length_mm * density
                except:
                    pass

        return total_weight


    def get_max_drift(self):
        """Return maximum story drift ratio, or 0 if drift check disabled."""
        if not self.config["ENABLE_DRIFT_CHECK"]:
            return 0.0
        try:
            story_drifts_table = self._resolve_table_name("Story Drifts")
            result = self.SapModel.DatabaseTables.GetTableForDisplayArray(
                story_drifts_table, [], "", 0, [], 0, [])
            table = self._normalize_table_result(result)
            if not table:
                return 999.0

            fields = table["fields"]
            num_rows = table["num_rows"]
            data = table["data"]

            fields_lower = [f.lower() for f in fields]
            drift_idx = next((i for i, f in enumerate(fields_lower) if "drift" in f), None)
            if drift_idx is None:
                return 999.0

            max_drift = 0.0
            width = table["width"]
            for r in range(num_rows):
                row = data[r*width : (r+1)*width]
                try:
                    val = self._coerce_float(row[drift_idx])
                    max_drift = max(max_drift, val)
                except:
                    pass
            return max_drift if max_drift > self.config["ALLOW_DRIFT"] else 0.0
        except Exception:
            return 999.0

    def compute_cost(self, beam_dims, col_dims, bc_grade, cc_grade, rebar_kg, steel_grade):
        """
        Returns (volume_m3, rebar_kg, total_currency, fitness).
        Fitness = volume_cost + concrete_cost + rebar_cost + penalties.
        """
        # Volume
        beam_area_m2 = (beam_dims[0] * beam_dims[1]) / 1e6
        col_area_m2 = (col_dims[0] * col_dims[1]) / 1e6
        vol_beam = beam_area_m2 * self.len_beams
        vol_col = col_area_m2 * self.len_cols
        total_vol = vol_beam + vol_col

        # Concrete cost
        conc_cost_beam = vol_beam * self.config["COST_PER_M3"] * self.config["CONC_COST_FACTORS"].get(bc_grade, 1.0)
        conc_cost_col = vol_col * self.config["COST_PER_M3"] * self.config["CONC_COST_FACTORS"].get(cc_grade, 1.0)
        conc_cost = conc_cost_beam + conc_cost_col

        # Rebar cost
        steel_factor = self.config["STEEL_COST_FACTORS"].get(steel_grade, 1.0)
        rebar_cost = rebar_kg * self.config["REBAR_COST_PER_KG"] * steel_factor

        total_cost = conc_cost + rebar_cost

        # Penalties
        penalty = 0
        # Soft max penalty
        for dim in beam_dims + col_dims:
            if dim > self.config["SOFT_MAX_DIM"]:
                excess = dim - self.config["SOFT_MAX_DIM"]
                penalty += self.config["PEN_SOFT_MAX"] * (excess / self.config["SOFT_MAX_DIM"])**2

        fitness = total_cost + penalty
        return total_vol, rebar_kg, total_cost, fitness

    def get_current_volume(self, verbose=False):
        """Compute total concrete volume from current model (for baseline comparison)."""
        total_vol = 0.0
        section_areas = {}
        if verbose:
            print(f"\n{'Frame':<15} | {'Area (m2)':<12} | {'Length (m)':<12} | {'Vol (m3)':<10}")
            print("-" * 55)

        for frame in self.beams + self.columns:
            try:
                prop_name = self.SapModel.FrameObj.GetSection(frame)[0]
                if prop_name in section_areas:
                    area_m2 = section_areas[prop_name]
                else:
                    # Assume rectangular
                    res = self.SapModel.PropFrame.GetRectangle(prop_name)
                    if len(res) >= 4:
                        h = res[2]
                        w = res[3]
                        area_m2 = (h * w) / 1e6
                        section_areas[prop_name] = area_m2
                    else:
                        print(f"[WARNING] Could not get rectangle for section {prop_name}")
                        continue
                length_m = self.get_frame_length(frame)
                vol = area_m2 * length_m
                total_vol += vol
                if verbose:
                    print(f"{frame:<15} | {area_m2:<12.6f} | {length_m:<12.3f} | {vol:<10.6f}")
            except Exception as e:
                if verbose:
                    print(f"[ERROR] Volume calc failed for {frame}: {e}")

        if verbose:
            print("-" * 55)
        return total_vol

    def get_baseline_individual(self):
        """Extract current design as an individual (if possible)."""
        try:
            # Assume first beam and column represent the whole group
            if not self.beams or not self.columns:
                return None
            b_sec = self.SapModel.FrameObj.GetSection(self.beams[0])[0]
            b_res = self.SapModel.PropFrame.GetRectangle(b_sec)
            if len(b_res) < 4:
                return None
            bh, bb = b_res[2], b_res[3]

            c_sec = self.SapModel.FrameObj.GetSection(self.columns[0])[0]
            c_res = self.SapModel.PropFrame.GetRectangle(c_sec)
            if len(c_res) < 4:
                return None
            ch, cb = c_res[2], c_res[3]

            # Get material grades (simplified: parse from section name or query)
            # For simplicity, use middle indices as fallback
            bci = len(self.config["CONC_GRADES"]) // 2
            cci = len(self.config["CONC_GRADES"]) // 2
            si = len(self.config["STEEL_GRADES"]) // 2

            return (self._snap(bh), self._snap(bb), self._snap(ch), self._snap(cb), bci, cci, si)
        except:
            return None

    def _snap(self, v):
        """Round to nearest step and clamp to bounds."""
        step = self.config["STEP_MM"]
        v = int(round(v / step) * step)
        return max(self.config["MM_MIN"], min(self.config["MM_MAX"], v))

# ============================================================================
# GENETIC ALGORITHM OPERATORS (improved)
# ============================================================================
def new_individual(config, rng):
    """Generate a random feasible individual."""
    mm_min = config["MM_MIN"]
    mm_max = config["MM_MAX"]
    step = config["STEP_MM"]
    aspect_max = config["ASPECT_MAX"]
    conc_grades = config["CONC_GRADES"]
    steel_grades = config["STEEL_GRADES"]

    attempts = 0
    while attempts < 1000:
        Bh = rng.randrange(mm_min, mm_max+step, step)
        Bb = rng.randrange(mm_min, mm_max+step, step)
        Ch = rng.randrange(mm_min, mm_max+step, step)
        Cb = rng.randrange(mm_min, mm_max+step, step)
        BCi = rng.randrange(len(conc_grades))
        CCi = rng.randrange(len(conc_grades))
        Si = rng.randrange(len(steel_grades))

        # Aspect ratio check
        if max(Bh/Bb, Bb/Bh) <= aspect_max and max(Ch/Cb, Cb/Ch) <= aspect_max:
            # Feasibility check
            if is_feasible(config, Bh, Bb, Ch, Cb, CCi):
                return (Bh, Bb, Ch, Cb, BCi, CCi, Si)
        attempts += 1

    # Fallback: square section with medium materials
    mid = mm_min + ((mm_max - mm_min) // (2*step)) * step
    return (mid, mid, mid, mid,
            len(conc_grades)//2, len(conc_grades)//2, len(steel_grades)//2)

def is_feasible(config, Bh, Bb, Ch, Cb, CCi):
    """Check if a design is physically plausible (simple axial capacity check)."""
    fc = config["CONC_GRADES"][CCi]
    Ag_col = Ch * Cb          # mm^2
    fc_eff = 0.85 * fc        # MPa
    min_cap_n = 500000        # 500 kN minimum (adjust as needed)
    if fc_eff * Ag_col < min_cap_n:
        return False
    # Beam minimum dimensions
    if Bh < 200 or Bb < 150:
        return False
    return True

def sbx_crossover(p1, p2, rng, config, eta=15):
    """
    Simulated Binary Crossover for real-valued dimensions.
    Returns two offspring.
    """
    child1 = list(p1)
    child2 = list(p2)
    for i in range(4):   # only dimensions
        if rng.random() < 0.5:
            u = rng.random()
            if u <= 0.5:
                beta = (2*u)**(1/(eta+1))
            else:
                beta = (1/(2*(1-u)))**(1/(eta+1))
            child1[i] = 0.5 * ((1+beta)*p1[i] + (1-beta)*p2[i])
            child2[i] = 0.5 * ((1-beta)*p1[i] + (1+beta)*p2[i])
            # Round to step
            child1[i] = round_to_step(child1[i], p1[i], p2[i], config)
            child2[i] = round_to_step(child2[i], p1[i], p2[i], config)
    # For discrete indices, uniform crossover
    for i in range(4, 7):
        if rng.random() < 0.5:
            child1[i] = p2[i]
            child2[i] = p1[i]
    return (tuple(child1), tuple(child2))

def round_to_step(val, parent1, parent2, config):
    """Round a real value to nearest step within bounds, using parent bounds."""
    step = config["STEP_MM"]
    mm_min = config["MM_MIN"]
    mm_max = config["MM_MAX"]
    # Round to nearest multiple of step
    rounded = int(round(val / step) * step)
    # Clamp to bounds
    return max(mm_min, min(mm_max, rounded))

def mutate_individual(ind, config, rng):
    """Polynomial mutation for dimensions, discrete for indices, with feasibility repair."""
    li = list(ind)
    mm_min = config["MM_MIN"]
    mm_max = config["MM_MAX"]
    step = config["STEP_MM"]
    mut_rate = config["MUT_RATE"]

    # Mutate dimensions (polynomial mutation)
    eta_m = 20
    for i in range(4):
        if rng.random() < mut_rate:
            y = li[i]
            yl = mm_min
            yu = mm_max
            delta1 = (y - yl) / (yu - yl)
            delta2 = (yu - y) / (yu - yl)
            rnd = rng.random()
            mut_pow = 1.0 / (eta_m + 1.0)
            if rnd < 0.5:
                xy = 1.0 - delta1
                val = 2.0 * rnd + (1.0 - 2.0 * rnd) * (xy ** (eta_m + 1.0))
                deltaq = val ** mut_pow - 1.0
            else:
                xy = 1.0 - delta2
                val = 2.0 * (1.0 - rnd) + 2.0 * (rnd - 0.5) * (xy ** (eta_m + 1.0))
                deltaq = 1.0 - val ** mut_pow
            y = y + deltaq * (yu - yl)
            li[i] = round_to_step(y, li[i], li[i], config)

    # Mutate material indices (discrete)
    for i in range(4, 7):
        if rng.random() < mut_rate:
            if i < 6:  # concrete indices
                li[i] = rng.randrange(len(config["CONC_GRADES"]))
            else:       # steel index
                li[i] = rng.randrange(len(config["STEEL_GRADES"]))

    # Enforce aspect ratio
    for (h_idx, w_idx) in [(0,1), (2,3)]:
        h, w = li[h_idx], li[w_idx]
        if max(h/w, w/h) > config["ASPECT_MAX"]:
            if h > w:
                li[h_idx] = int(round(h / config["ASPECT_MAX"] / step) * step)
            else:
                li[w_idx] = int(round(w / config["ASPECT_MAX"] / step) * step)

    # Feasibility repair: if column too weak, increase grade or dimensions
    repair_attempts = 0
    max_grade_idx = len(config["CONC_GRADES"]) - 1
    while not is_feasible(config, li[0], li[1], li[2], li[3], li[5]) and repair_attempts < 10:
        # Try increasing column concrete grade
        if li[5] < max_grade_idx:
            li[5] += 1
        else:
            # Increase column dimensions by one step
            li[2] = min(li[2] + step, mm_max)
            li[3] = min(li[3] + step, mm_max)
        repair_attempts += 1

    return tuple(li)

# ============================================================================
# MAIN OPTIMIZATION LOOP
# ============================================================================
def main_loop(gui_params=None, log_callback=None, data_callback=None):
    # Merge GUI parameters with CONFIG
    config = CONFIG.copy()
    if gui_params:
        config.update(gui_params)

    # Setup logging
    def log(msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] {msg}"
        if log_callback:
            log_callback(full_msg)
        else:
            try:
                print(full_msg)
            except OSError:
                sys.stdout.write(full_msg + "\n")
                sys.stdout.flush()

    # Initialize ETABS client
    etabs = EtabsClient(config)
    etabs.connect()
    try:
        model_to_open = etabs.prepare_model_copy(config["BASE_EDB"])
    except Exception as e:
        log(f"Failed to prepare model copy: {e}")
        return

    if not etabs.open_model(model_to_open):
        log("Failed to open model. Exiting.")
        return
    log(f"Working model: {etabs.model_path}")

    etabs.scan_frames()
    if not etabs.beams or not etabs.columns:
        log("No beams or columns found. Exiting.")
        return

    orig_vol = etabs.get_current_volume(verbose=False)
    log(f"Original total concrete volume: {orig_vol:.3f} m^3")
    if orig_vol == 0:
        log("WARNING: Original volume is zero. Check that model has sections assigned and they are rectangular.")

    etabs.calculate_total_lengths()

    # Seed population with baseline if available
    rng = random.Random(42)  # deterministic for reproducibility
    pop_size = config["POP"]
    baseline = etabs.get_baseline_individual()
    pop = [new_individual(config, rng) for _ in range(pop_size)]
    if baseline:
        pop[0] = baseline
        log(f"Seeded with baseline: {baseline}")

    best_individual = None
    best_fitness = float('inf')
    best_passed_individual = None
    best_passed_fitness = float('inf')
    best_passed_vol = None  # to store volume of best passed

    elite_count = config["ELITE_COUNT"]
    parent_count = config["PARENTS"]

    for gen in range(config["GENS"]):
        if etabs.stop_requested:
            log("Optimization stopped by user.")
            break

        log(f"\n=== Generation {gen+1}/{config['GENS']} ===")
        scored = []

        for idx, ind in enumerate(pop):
            if etabs.stop_requested:
                break

            # Unpack
            Bh, Bb, Ch, Cb, BCi, CCi, Si = ind
            bc_grade = config["CONC_GRADES"][BCi]
            cc_grade = config["CONC_GRADES"][CCi]
            steel_grade = config["STEEL_GRADES"][Si]

            # Calculate Elastic Modulus (GPa) for logging
            ec_b = 4700 * (bc_grade**0.5) / 1000
            ec_c = 4700 * (cc_grade**0.5) / 1000

            log(f"  Individual {idx}: Beam {Bh}x{Bb} (C{bc_grade}, Ec={ec_b:.1f}GPa), "
                f"Column {Ch}x{Cb} (C{cc_grade}, Ec={ec_c:.1f}GPa), Steel S{steel_grade}")
            # Define materials (cached)

            bc_mat = etabs.define_concrete(bc_grade)
            cc_mat = etabs.define_concrete(cc_grade)
            steel_mat = etabs.define_rebar(steel_grade)

            # Define sections
            beam_sec_name = etabs.beam_work_section or "OPT_BEAM_SECTION"
            col_sec_name = etabs.column_work_section or "OPT_COLUMN_SECTION"
            etabs.define_rect_section(beam_sec_name, bc_mat, Bh, Bb)
            etabs.define_rect_section(col_sec_name, cc_mat, Ch, Cb)
            if not etabs.apply_section_rebar(beam_sec_name, steel_mat, is_beam=True):
                log(f"WARNING: Could not assign beam rebar material {steel_mat} to section {beam_sec_name}.")
            if not etabs.apply_section_rebar(col_sec_name, steel_mat, is_beam=False):
                log(f"WARNING: Could not assign column rebar material {steel_mat} to section {col_sec_name}.")

            # Assign sections
            etabs.assign_section_to_frames(etabs.beams, beam_sec_name)
            etabs.assign_section_to_frames(etabs.columns, col_sec_name)

            # Run analysis & design
            start_time = time.time()
            success = etabs.run_analysis_design()

            if not success:
                # Analysis failed -> huge penalty
                fitness = config["PEN_FAIL"] * 1e6
                vol = 0.0
                rebar_kg = 0.0
                total_cost = 0.0
                fails = 999
                drift = 999
            else:
                try:
                    fails, rebar_kg = etabs.verify_design()
                    drift = etabs.get_max_drift()
                    vol, rebar_kg, total_cost, fitness = etabs.compute_cost(
                        (Bh, Bb), (Ch, Cb), bc_grade, cc_grade, rebar_kg, steel_grade
                    )

                    # Add penalties for failures and drift
                    if fails > 0:
                        fitness += fails * config["PEN_FAIL"]
                    if drift > config["ALLOW_DRIFT"]:
                        fitness += config["PEN_DRIFT"]
                except Exception as e:
                    log(f"    -> Verification failed: {e}")
                    fitness = config["PEN_FAIL"] * 1e6
                    vol = 0.0
                    rebar_kg = 0.0
                    total_cost = 0.0
                    fails = 999
                    drift = 999

            elapsed = time.time() - start_time
            log(f"    -> Vol={vol:.2f} m^3 (C{bc_grade}/C{cc_grade}), Rebar={rebar_kg:.1f} kg (S{steel_grade}), Cost={total_cost:,.0f}, "
                f"Fails={fails}, Drift={drift:.4f}, Fitness={fitness:.0f} ({elapsed:.1f}s)")


            scored.append({
                "ind": ind,
                "fitness": fitness,
                "vol": vol,
                "cost": total_cost,
                "fails": fails,
                "drift": drift
            })

            # Update best overall
            if fitness < best_fitness:
                best_fitness = fitness
                best_individual = ind

            # Update best feasible (no fails, drift ok)
            if fails == 0 and (drift <= config["ALLOW_DRIFT"] or not config["ENABLE_DRIFT_CHECK"]):
                if fitness < best_passed_fitness:
                    best_passed_fitness = fitness
                    best_passed_individual = ind
                    best_passed_vol = vol
                    log(f"    *** NEW BEST FEASIBLE ***")

            # Send data to GUI if callback provided
            if data_callback:
                data_callback(gen+1, total_cost, fitness, ind)

        # Evolution (skip last generation)
        if gen < config["GENS"] - 1 and not etabs.stop_requested:
            # Sort by fitness
            scored.sort(key=lambda x: x["fitness"])

            # Elitism: keep top elite_count
            new_pop = [scored[i]["ind"] for i in range(min(elite_count, len(scored)))]

            # Create mating pool from top parents
            parents = [scored[i]["ind"] for i in range(min(parent_count, len(scored)))]

            # Fill rest with crossover + mutation
            while len(new_pop) < pop_size:
                p1 = rng.choice(parents)
                p2 = rng.choice(parents)
                # Crossover
                if rng.random() < 0.9:  # crossover probability
                    c1, c2 = sbx_crossover(p1, p2, rng, config)
                    child = c1 if rng.random() < 0.5 else c2
                else:
                    child = p1  # asexual reproduction
                # Mutate
                child = mutate_individual(child, config, rng)
                new_pop.append(child)

            pop = new_pop

    # Final summary
    log("\n" + "="*50)
    log("OPTIMIZATION COMPLETE")
    log("="*50)
    log(f"Original Volume: {orig_vol:.3f} m^3")

    if best_passed_individual:
        Bh, Bb, Ch, Cb, BCi, CCi, Si = best_passed_individual
        log(f"Best Feasible Design (Fitness {best_passed_fitness:.0f}):")
        log(f"  Beam: {Bh}x{Bb} (C{config['CONC_GRADES'][BCi]})")
        log(f"  Column: {Ch}x{Cb} (C{config['CONC_GRADES'][CCi]})")
        log(f"  Rebar Grade: S{config['STEEL_GRADES'][Si]}")
        log(f"  Volume: {best_passed_vol:.3f} m^3")
    else:
        log("No fully feasible design found. Best overall:")
        if best_individual:
            Bh, Bb, Ch, Cb, BCi, CCi, Si = best_individual
            log(f"  Beam: {Bh}x{Bb} (C{config['CONC_GRADES'][BCi]})")
            log(f"  Column: {Ch}x{Cb} (C{config['CONC_GRADES'][CCi]})")
            log(f"  Rebar Grade: S{config['STEEL_GRADES'][Si]}")
            # Compute volume for best overall (rebar not needed for volume)
            vol_best = (Bh*Bb/1e6)*etabs.len_beams + (Ch*Cb/1e6)*etabs.len_cols
            log(f"  Volume: {vol_best:.3f} m^3")
        else:
            log("No designs evaluated.")

    # Apply best design to model (if feasible or user wants)
    target = best_passed_individual if best_passed_individual else best_individual
    if target and not etabs.stop_requested:
        log("\nApplying best design to model...")
        Bh, Bb, Ch, Cb, BCi, CCi, Si = target
        bc_grade = config["CONC_GRADES"][BCi]
        cc_grade = config["CONC_GRADES"][CCi]
        steel_grade = config["STEEL_GRADES"][Si]

        final_beam_sec = etabs.create_final_section(
            "FINAL_BEAM", (Bh, Bb), bc_grade, steel_grade, is_beam=True
        )
        final_col_sec = etabs.create_final_section(
            "FINAL_COL", (Ch, Cb), cc_grade, steel_grade, is_beam=False
        )
        etabs.assign_section_to_frames(etabs.beams, final_beam_sec)
        etabs.assign_section_to_frames(etabs.columns, final_col_sec)

        log("Final model updated. Running verification...")
        etabs.run_analysis_design()
        fails, _ = etabs.verify_design()
        if fails == 0:
            log("SUCCESS: Final design passes all checks.")
        else:
            log(f"WARNING: Final design has {fails} failed members. Manual review needed.")

        saved = etabs.save_model()
        if saved:
            log(f"Saved working model: {etabs.model_path}")
        else:
            log("WARNING: Could not save the working model copy.")

        log(f"Final ETABS beam section name: {final_beam_sec}")
        log(f"Final ETABS column section name: {final_col_sec}")
    else:
        log("No design applied.")

    return etabs

if __name__ == "__main__":
    main_loop()


