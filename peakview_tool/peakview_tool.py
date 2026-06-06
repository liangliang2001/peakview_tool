#!/usr/bin/env python
"""PeakView automation helper.

This CLI wraps the PeakDesign scripting API through an already-running
virtuoso-bridge session. It is intentionally small: the remote PeakDesign script
does the EDA work, while this local wrapper handles transport, polling, result
download, plotting, and optional Cadence sync.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


DEFAULT_BRIDGE_ROOT = Path(os.environ.get("PEAKVIEW_BRIDGE_ROOT", r"E:\Agent\virtuoso-bridge-lite-main"))
DEFAULT_PROJECT = "/T22_Codex/Codex_Lib/.peakview"
DEFAULT_PROFILE = "RC_IRCX_CLN22ULP_1P8M+UT-ALRDL_5X1Z1U_typical(DRM:T-N22-CL-DR-001 v1.5)"
DEFAULT_LIB = "Codex_Lib"
DEFAULT_PEAKDESIGN = "/data/eda/lorentz/peakview/bin/peakdesign"
DEFAULT_PEAKPYTHON = "/data/eda/lorentz/peakview/bin/peakpython"
DEFAULT_CATALOG = Path(__file__).with_name("pcircuit_catalog.json")


DOCUMENTED_API_SURFACE = [
    "openProject",
    "project.createNewProject",
    "saveProject",
    "Project.save",
    "Project.close",
    "Project.createCellFromLayout",
    "Project.createCellFromGDS",
    "Project.createCell",
    "Project.getCellByName",
    "Cell.getName",
    "Cell.getType",
    "Cell.getOptions",
    "Cell.getParameters",
    "Cell.getSweepParameters",
    "Cell.getSimulationOptions",
    "Cell.getPbmParameters",
    "Project.setCellParameters",
    "Cell.setCellParameters",
    "Project.setCellOptions",
    "Project.setCellSimulationOptions",
    "Project.setCellPbmParameters",
    "Project.collectGeneratedCells",
    "Project.copyCell",
    "Project.renameCell",
    "Project.deleteCell",
    "Project.genSkillCode",
    "Cell.addInstance",
    "Instance.getName",
    "Instance.getParameters",
    "Cell.findInstanceByName",
    "Instance.findInstanceByName",
    "Instance.setParameters",
    "Cell.deleteInstance",
    "loadPcircuit",
    "simulate",
    "waitForResult",
    "exit",
]


def _import_bridge(bridge_root: Path):
    """Import virtuoso_bridge without requiring callers to activate its venv."""
    src = bridge_root / "src"
    if src.exists():
        sys.path.insert(0, str(src))

    # The bridge package installs a command log FileHandler at import time.
    # On this workstation that file is sometimes locked; disable that handler
    # for this wrapper so automation does not fail before connecting.
    logging.FileHandler = lambda *a, **k: logging.NullHandler()  # type: ignore[assignment]
    from virtuoso_bridge import VirtuosoClient  # type: ignore

    return VirtuosoClient


def _skill_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _decode_skill_string(value: str | None) -> str:
    if not value:
        return ""
    text = value
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            return ast.literal_eval(text)
        except Exception:
            return text[1:-1]
    return text


def _script_write_expr(remote_path: str, lines: list[str]) -> str:
    writes = []
    for line in lines:
        writes.append(f'fprintf(fp "{_skill_escape(line).replace("%", "%%")}\\n")')
    return f'let((fp) fp=outfile("{_skill_escape(remote_path)}" "w") {" ".join(writes)} close(fp))'


def _safe_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:80] or "peakview_job"


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class PeakViewBridge:
    def __init__(self, bridge_root: Path):
        VirtuosoClient = _import_bridge(bridge_root)
        self.client = VirtuosoClient.from_env()

    def skill(self, expr: str, timeout: int = 60):
        return self.client.execute_skill(expr, timeout=timeout)

    def remote_system(self, cmd: str, timeout: int = 60) -> str:
        log = f"/tmp/pkv_tool_shell_{int(time.time() * 1000)}.log"
        esc = _skill_escape(f"{cmd} > {log} 2>&1")
        expr = (
            f'let((fp line out) system("{esc}") fp=infile("{log}") out="" '
            'when(fp while(gets(line fp) out=strcat(out line)) close(fp)) out)'
        )
        result = self.skill(expr, timeout=timeout)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Remote shell failed: {result.errors}")
        return _decode_skill_string(result.output)

    def read_remote_text(self, path: str, timeout: int = 30) -> str:
        expr = (
            f'let((fp line out) fp=infile("{_skill_escape(path)}") out="" '
            'when(fp while(gets(line fp) out=strcat(out line)) close(fp)) out)'
        )
        result = self.skill(expr, timeout=timeout)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Read failed for {path}: {result.errors}")
        return _decode_skill_string(result.output)

    def write_remote_script(self, path: str, lines: list[str], timeout: int = 30) -> None:
        result = self.skill(_script_write_expr(path, lines), timeout=timeout)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Could not write remote script {path}: {result.errors}")

    def run_peakdesign_script(
        self,
        name: str,
        lines: list[str],
        *,
        project: str,
        peakdesign: str = DEFAULT_PEAKDESIGN,
        timeout: int = 2400,
        poll: int = 10,
    ) -> tuple[str, str, str]:
        tag = _safe_tag(name)
        script_path = f"/tmp/{tag}.py"
        marker_path = f"/tmp/{tag}.marker"
        log_path = f"/tmp/{tag}.log"
        result_path = f"/tmp/{tag}_results.json"
        self.write_remote_script(script_path, lines)

        # Clear stale lock only when no PeakDesign process is running. A live GUI
        # owns the lock and should not be disturbed.
        cmd = (
            f"rm -f {marker_path} {log_path} {result_path}; "
            f"if ! ps -u $USER -f | grep -q '[d]esignapp.pyc'; then rm -f {project}/pkv.lock; fi; "
            f"cd {Path(project).parent.as_posix()}; "
            f"DISPLAY=${{DISPLAY:-:7}} {peakdesign} --silent --script {script_path} > {log_path} 2>&1 &"
        )
        result = self.skill(f'system("{_skill_escape(cmd)}")', timeout=20)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Could not launch PeakDesign: {result.errors}")

        deadline = time.time() + timeout
        marker = ""
        log = ""
        while time.time() < deadline:
            time.sleep(poll)
            marker = self.read_remote_text(marker_path)
            log = self.read_remote_text(log_path)
            if "json_written" in marker or "Traceback" in marker:
                break

        if "Traceback" in marker:
            raise RuntimeError(f"PeakDesign script failed:\n{marker}\n\nPeakDesign log:\n{log}")
        if "json_written" not in marker:
            raise TimeoutError(f"PeakDesign script timed out. Marker:\n{marker}\n\nLog:\n{log}")
        return marker, log, result_path

    def load_skill(self, path: str, timeout: int = 300) -> str:
        result = self.skill(f'load("{_skill_escape(path)}")', timeout=timeout)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Cadence sync load failed: {result.errors}")
        return result.output or ""

    def verify_cell_views(self, lib: str, cell: str) -> dict[str, Any]:
        expr = (
            f'let((cell out) cell=ddGetObj("{_skill_escape(lib)}" "{_skill_escape(cell)}") '
            'out=nil if(cell then foreach(view cell~>views out=cons(view~>name out))) reverse(out))'
        )
        views = _decode_skill_string(self.skill(expr, timeout=60).output)
        layout_expr = (
            f'let((cv) cv=dbOpenCellViewByType("{_skill_escape(lib)}" "{_skill_escape(cell)}" '
            '"layout" "maskLayout" "r") if(cv then list(length(cv~>shapes) length(cv~>terminals)) else nil))'
        )
        layout = _decode_skill_string(self.skill(layout_expr, timeout=60).output)
        return {"views": views, "layout_summary": layout}

    def open_layout(self, lib: str, cell: str) -> str:
        result = self.client.open_window(lib, cell, view="layout", timeout=60)
        if result.status.name != "SUCCESS":
            raise RuntimeError(f"Could not open layout: {result.errors}")
        return result.output or ""


def peakdesign_script_for_design(args: argparse.Namespace, remote_result: str) -> list[str]:
    formulas = [
        ("Ld12_H", "Ld(1,2)"),
        ("Qd12", "Qd(1,2)"),
        ("Rse12_ohm", "Rse(1,2)"),
        ("Lse12_H", "Lse(1,2)"),
        ("Qse12", "Qse(1,2)"),
    ]
    if args.formula:
        for item in args.formula:
            key = _safe_tag(item)
            formulas.append((key, item))

    lines = [
        "showGUI(False)",
        "import json, time",
        f"project_dir={args.project!r}",
        f"profile={args.profile!r}",
        f"cell_name={args.cell!r}",
        f"pcircuit={args.pcircuit!r}",
        f"lib_name={args.lib!r}",
        f"replace_existing={bool(args.replace)!r}",
        f"simulate_enabled={bool(args.simulate)!r}",
        f"sync_enabled={bool(args.sync or args.generate_sync)!r}",
        f"remote_result={remote_result!r}",
        "f=open('/tmp/' + cell_name + '_pkv_tool.marker','w')",
        "try:",
        "    project=openProject(dir=project_dir, profile=profile)",
        "    f.write('opened %s\\n' % profile); f.flush()",
        "    base_name=cell_name",
        "    old=project.getCellByName(cell_name)",
        "    if old and replace_existing:",
        "        project.deleteCell(cell_name)",
        "        f.write('deleted_old\\n'); f.flush()",
        "    elif old:",
        "        cell_name='%s_%s' % (base_name, time.strftime('%Y%m%d_%H%M%S'))",
        "        f.write('renamed_to %s\\n' % cell_name); f.flush()",
        f"    freq={{'start':{args.freq_start!r},'stop':{args.freq_stop!r},'points':{str(args.points)!r},'rfic_option':{args.sweep_accuracy!r},'lin_log_choice':{args.scale!r}}}",
        "    project.setOptions(freq)",
        f"    cell=project.createCell(pcircuit=pcircuit, name=cell_name, itype={args.itype!r})",
        "    f.write('created %s\\n' % repr(cell)); f.flush()",
        "    f.write('params %s\\n' % repr(cell.getParameters())); f.flush()",
        "    project.save()",
        "    formulas={}",
        "    if simulate_enabled:",
        "        simulate(cell=cell_name)",
        "        f.write('simulate_started\\n'); f.flush()",
        f"        waitForResult(cell=cell_name, timeout={int(args.sim_timeout)})",
        "        f.write('simulate_done\\n'); f.flush()",
        "        for key, formula in %r:" % formulas,
        "            try:",
        "                formulas[key]=project.eval_formula_for_cell(cell_name, {'formula': formula})",
        "            except Exception as e:",
        "                formulas[key]={'error':repr(e)}",
        "    skill_path=None",
        "    if sync_enabled:",
        "        skill_path=project.genSkillCode(cell=cell_name, lib=lib_name)",
        "        f.write('skill_path %s\\n' % skill_path); f.flush()",
        "    out={'profile':profile,'project':project_dir,'lib':lib_name,'cell':cell_name,'pcircuit':pcircuit,'params':cell.getParameters(),'simopts':cell.getSimulationOptions(),'freq_options':freq,'formulas':formulas,'skill_path':skill_path,'nport':project_dir+'/'+cell_name+'/nport/text.txt','simulation_dir':project_dir+'/.peakdesign/simulation/'+cell_name}",
        "    try:",
        "        q=formulas.get('Qd12',{}).get('vals')",
        "        l=formulas.get('Ld12_H',{}).get('vals')",
        "        r=formulas.get('Rse12_ohm',{}).get('vals')",
        "        freqs=formulas.get('Ld12_H',{}).get('freqs')",
        "        if q and l and freqs:",
        "            imax=max(range(len(q)), key=lambda i:q[i])",
        "            out['summary']={'num_points':len(freqs),'f_start_Hz':freqs[0],'f_stop_Hz':freqs[-1],'max_Q':{'Q':q[imax],'f_Hz':freqs[imax],'L_nH':l[imax]*1e9,'R_ohm':r[imax] if r else None}}",
        "    except Exception as e:",
        "        out['summary_error']=repr(e)",
        "    open(remote_result,'w').write(json.dumps(out, indent=2, sort_keys=True))",
        "    f.write('json_written\\n'); f.flush()",
        "    project.save()",
        "    project.close(exitApp=True)",
        "except Exception:",
        "    import traceback; f.write(traceback.format_exc()); f.flush()",
        "f.close()",
        "exit(keepGUI=False)",
    ]
    return lines


def default_api_smoke_recipe(cell_prefix: str) -> dict[str, Any]:
    """A conservative recipe that exercises the generic API dispatcher."""
    cell = f"{cell_prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    copy_cell = f"{cell}_copy"
    renamed_cell = f"{cell}_renamed"
    return {
        "name": "peakview_api_smoke",
        "cleanup_on_success": True,
        "steps": [
            {
                "label": "set_frequency_options",
                "api": "Project.setOptions",
                "args": [
                    {
                        "start": "1.000e+09",
                        "stop": "2.000e+09",
                        "points": "5",
                        "rfic_option": "RFIC high",
                        "lin_log_choice": "lin",
                    }
                ],
            },
            {
                "label": "create_line_cell",
                "api": "Project.createCell",
                "kwargs": {"pcircuit": "Line", "name": cell, "itype": "PL"},
                "save_as": "cell",
            },
            {"label": "cell_name", "api": "Cell.getName", "target": "$cell"},
            {"label": "cell_type", "api": "Cell.getType", "target": "$cell"},
            {"label": "cell_options", "api": "Cell.getOptions", "target": "$cell"},
            {"label": "cell_parameters", "api": "Cell.getParameters", "target": "$cell"},
            {"label": "cell_sweep_parameters", "api": "Cell.getSweepParameters", "target": "$cell"},
            {"label": "cell_simulation_options", "api": "Cell.getSimulationOptions", "target": "$cell"},
            {"label": "cell_pbm_parameters", "api": "Cell.getPbmParameters", "target": "$cell"},
            {"label": "get_cell_by_name", "api": "Project.getCellByName", "args": [cell], "save_as": "cell_lookup"},
            {"label": "copy_cell", "api": "Project.copyCell", "kwargs": {"source": cell, "destination": copy_cell}},
            {"label": "rename_cell", "api": "Project.renameCell", "kwargs": {"cell": copy_cell, "newName": renamed_cell}},
            {"label": "collect_generated_cells", "api": "Project.collectGeneratedCells", "kwargs": {"cell": cell}},
            {"label": "generate_skill", "api": "Project.genSkillCode", "kwargs": {"cell": cell, "lib": DEFAULT_LIB}},
            {"label": "save_project_method", "api": "Project.save"},
            {"label": "save_project_function", "api": "saveProject"},
        ],
        "cleanup": [
            {"api": "Project.deleteCell", "args": [cell]},
            {"api": "Project.deleteCell", "args": [renamed_cell]},
            {"api": "Project.save"},
        ],
    }


def peakdesign_script_for_api(recipe: dict[str, Any], remote_result: str, marker_path: str) -> list[str]:
    recipe_text = json.dumps(recipe, ensure_ascii=True, sort_keys=True)
    lines = [
        "showGUI(False)",
        "import json, traceback",
        f"recipe=json.loads({recipe_text!r})",
        f"remote_result={remote_result!r}",
        f"marker_path={marker_path!r}",
        "marker=open(marker_path,'w')",
        "objects={}",
        "results=[]",
        "cleanup_results=[]",
        "pv_project=None",
        "closed=False",
        "DOCUMENTED_API_SURFACE=%r" % DOCUMENTED_API_SURFACE,
        "",
        "try:",
        "    unicode",
        "except NameError:",
        "    unicode=str",
        "",
        "def plain(value):",
        "    if isinstance(value, unicode):",
        "        return str(value)",
        "    if isinstance(value, list):",
        "        return [plain(v) for v in value]",
        "    if isinstance(value, dict):",
        "        return {plain(k): plain(v) for k, v in value.items()}",
        "    return value",
        "",
        "recipe=plain(recipe)",
        "",
        "def note(msg):",
        "    marker.write(str(msg)+'\\n')",
        "    marker.flush()",
        "",
        "def jsonable(value):",
        "    if value is None or isinstance(value, (str, int, float, bool)):",
        "        return value",
        "    if isinstance(value, (list, tuple)):",
        "        return [jsonable(v) for v in value]",
        "    if isinstance(value, dict):",
        "        return {str(k): jsonable(v) for k, v in value.items()}",
        "    out={'repr': repr(value), 'type': value.__class__.__name__}",
        "    for meth in ('getName', 'getType'):",
        "        if hasattr(value, meth):",
        "            try:",
        "                out[meth]=jsonable(getattr(value, meth)())",
        "            except Exception as e:",
        "                out[meth+'_error']=repr(e)",
        "    return out",
        "",
        "def resolve(value):",
        "    if isinstance(value, str) and value.startswith('$'):",
        "        return objects[value[1:]]",
        "    if isinstance(value, list):",
        "        return [resolve(v) for v in value]",
        "    if isinstance(value, dict):",
        "        return {k: resolve(v) for k, v in value.items()}",
        "    return value",
        "",
        "def default_target(api):",
        "    if api.startswith('Project.') or api.startswith('project.'):",
        "        return 'project'",
        "    if api.startswith('Cell.') or api in ('Instance.findInstanceByName',):",
        "        return 'cell'",
        "    if api.startswith('Instance.'):",
        "        return 'instance'",
        "    return None",
        "",
        "def call_api(step):",
        "    global pv_project, closed",
        "    api=step['api']",
        "    args=resolve(step.get('args', []))",
        "    kwargs=resolve(step.get('kwargs', {}))",
        "    target=step.get('target')",
        "    if api in ('Project.setCellParameters', 'Cell.setCellParameters'):",
        "        api='Project.setCellParameters'",
        "        target='project'",
        "    if api == 'Instance.findInstanceByName':",
        "        api='Cell.findInstanceByName'",
        "        target=target or 'cell'",
        "    if api == 'openProject':",
        "        _opened_project=openProject(*args, **kwargs)",
        "        if _opened_project is not None:",
        "            pv_project=_opened_project",
        "        else:",
        "            pv_project=globals().get('project')",
        "        if pv_project is None:",
        "            raise RuntimeError('openProject did not return or expose a project object')",
        "        objects['project']=pv_project",
        "        return pv_project",
        "    if api == 'saveProject':",
        "        return saveProject(pv_project, *args, **kwargs)",
        "    if api == 'exit':",
        "        closed=True",
        "        return exit(*args, **kwargs)",
        "    if '.' in api:",
        "        owner, method=api.split('.', 1)",
        "        target=target or default_target(api)",
        "        if target is None:",
        "            raise RuntimeError('No default target for %s' % api)",
        "        obj=resolve(target) if isinstance(target, str) and target.startswith('$') else objects[target]",
        "        return getattr(obj, method)(*args, **kwargs)",
        "    return globals()[api](*args, **kwargs)",
        "",
        "def run_step(step, bucket):",
        "    item={'label': step.get('label'), 'api': step.get('api')}",
        "    try:",
        "        value=call_api(step)",
        "        if step.get('save_as'):",
        "            objects[step['save_as']]=value",
        "        item['ok']=True",
        "        item['result']=jsonable(value)",
        "        note('ok %s' % (step.get('label') or step.get('api')))",
        "    except Exception as e:",
        "        item['ok']=False",
        "        item['error']=traceback.format_exc()",
        "        note('error %s %s' % (step.get('label') or step.get('api'), repr(e)))",
        "        if step.get('optional') or step.get('allow_error'):",
        "            pass",
        "        else:",
        "            bucket.append(item)",
        "            raise",
        "    bucket.append(item)",
        "    return item",
        "",
        "try:",
        "    project_dir=recipe.get('project')",
        "    profile=recipe.get('profile')",
        "    if recipe.get('open', True):",
        "        _opened_project=openProject(dir=project_dir, profile=profile)",
        "        if _opened_project is not None:",
        "            pv_project=_opened_project",
        "        else:",
        "            pv_project=globals().get('project')",
        "        if pv_project is None:",
        "            raise RuntimeError('openProject did not return or expose a project object')",
        "        objects['project']=pv_project",
        "        note('opened %s' % profile)",
        "    for step in recipe.get('steps', []):",
        "        run_step(step, results)",
        "    if recipe.get('cleanup_on_success'):",
        "        for step in recipe.get('cleanup', []):",
        "            step=dict(step)",
        "            step['optional']=True",
        "            run_step(step, cleanup_results)",
        "    out={'ok': True, 'recipe': recipe.get('name'), 'project': project_dir, 'profile': profile, 'documented_api_surface': DOCUMENTED_API_SURFACE, 'steps': results, 'cleanup': cleanup_results}",
        "    open(remote_result,'w').write(json.dumps(out, indent=2, sort_keys=True))",
        "    note('json_written')",
        "    if pv_project is not None and not closed:",
        "        pv_project.close(exitApp=True)",
        "except Exception:",
        "    out={'ok': False, 'recipe': recipe.get('name'), 'steps': results, 'cleanup': cleanup_results, 'error': traceback.format_exc(), 'documented_api_surface': DOCUMENTED_API_SURFACE}",
        "    open(remote_result,'w').write(json.dumps(out, indent=2, sort_keys=True))",
        "    note(traceback.format_exc())",
        "    note('json_written')",
        "    try:",
        "        if pv_project is not None and not closed:",
        "            pv_project.close(exitApp=True)",
        "    except Exception:",
        "        pass",
        "marker.close()",
        "exit(keepGUI=False)",
    ]
    return lines


def peakdesign_script_for_models(args: argparse.Namespace, remote_result: str, marker_path: str) -> list[str]:
    return [
        "showGUI(False)",
        "import json, traceback",
        "import circuit",
        f"project_dir={args.project!r}",
        f"profile={args.profile!r}",
        f"remote_result={remote_result!r}",
        f"marker_path={marker_path!r}",
        "marker=open(marker_path,'w')",
        "",
        "def jsonable(value):",
        "    try:",
        "        json.dumps(value)",
        "        return value",
        "    except Exception:",
        "        return repr(value)",
        "",
        "try:",
        "    project=openProject(dir=project_dir, profile=profile)",
        "    macro_devices=getattr(circuit, 'macroDevices', {})",
        "    cdf_devices=getattr(circuit, 'cdfDevices', {})",
        "    display_map=getattr(circuit, 'macroDisplay2DeivceNames', {})",
        "    dynamic_list=getattr(circuit, 'DPcircuitList', [])",
        "    registered=[]",
        "    registered_with_plem=[]",
        "    for name in sorted(macro_devices.keys()):",
        "        meta=macro_devices.get(name) or {}",
        "        params=meta.get('para', [])",
        "        item={'name': name, 'parameters': jsonable(params), 'parameter_count': len(params) if hasattr(params, '__len__') else None, 'icon_path': jsonable(meta.get('icon_path')), 'em_simulation_type': jsonable(meta.get('em_simulation_type')), 'synthesis': jsonable(meta.get('syn_para')), 'is_cdf': name in cdf_devices, 'is_dynamic': name in dynamic_list, 'is_plem_variant': name.endswith('___PLEM__')}",
        "        registered_with_plem.append(item)",
        "        if not item['is_plem_variant']:",
        "            registered.append(item)",
        "    display=[]",
        "    for display_name in sorted(display_map.keys()):",
        "        internal=display_map[display_name]",
        "        meta=macro_devices.get(internal, macro_devices.get(display_name, {})) or {}",
        "        params=meta.get('para', [])",
        "        display.append({'display_name': display_name, 'internal_name': internal, 'parameters': jsonable(params), 'parameter_count': len(params) if hasattr(params, '__len__') else None, 'icon_path': jsonable(meta.get('icon_path')), 'em_simulation_type': jsonable(meta.get('em_simulation_type')), 'synthesis': jsonable(meta.get('syn_para')), 'is_dynamic': display_name in dynamic_list or internal in dynamic_list})",
        "    out={'ok': True, 'project': project_dir, 'profile': profile, 'source': 'PeakDesign runtime circuit.macroDevices', 'counts': {'display_models': len(display), 'registered_models': len(registered), 'registered_with_plem': len(registered_with_plem), 'dynamic_models': len(dynamic_list)}, 'display_models': display, 'registered_models': registered, 'registered_models_with_plem': registered_with_plem, 'dynamic_models': sorted(dynamic_list)}",
        "    open(remote_result,'w').write(json.dumps(out, indent=2, sort_keys=True))",
        "    marker.write('json_written\\n'); marker.flush()",
        "    project.close(exitApp=True)",
        "except Exception:",
        "    open(remote_result,'w').write(json.dumps({'ok': False, 'error': traceback.format_exc()}, indent=2, sort_keys=True))",
        "    marker.write(traceback.format_exc()); marker.write('json_written\\n'); marker.flush()",
        "marker.close()",
        "exit(keepGUI=False)",
    ]


def make_plot(json_path: Path, png_path: Path | None = None) -> Path | None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    formulas = data.get("formulas", {})
    ldat = formulas.get("Ld12_H") or formulas.get("Lse12_H") or {}
    qdat = formulas.get("Qd12") or formulas.get("Qse12") or {}
    rdat = formulas.get("Rse12_ohm") or {}
    freqs = ldat.get("freqs")
    lvals = ldat.get("vals")
    qvals = qdat.get("vals")
    rvals = rdat.get("vals")
    if not (freqs and lvals and qvals):
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    x = [float(v) / 1e9 for v in freqs]
    l_nh = [float(v) * 1e9 for v in lvals]
    if png_path is None:
        png_path = json_path.with_suffix(".png")

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(3, 1, figsize=(10, 11), sharex=True)
    fig.suptitle(f"PeakView {data.get('pcircuit')} Simulation", fontsize=16, fontweight="bold")
    fig.text(0.5, 0.945, f"{data.get('cell')} | {data.get('profile')}", ha="center", fontsize=8.5, color="#444")
    axes[0].plot(x, l_nh, color="#1f77b4", lw=2)
    axes[0].axhline(0, color="#777", lw=0.8)
    axes[0].set_ylabel("Ld/Lse (nH)")
    axes[0].set_title("Inductance")
    axes[1].plot(x, qvals, color="#d62728", lw=2)
    axes[1].axhline(0, color="#777", lw=0.8)
    axes[1].set_ylabel("Q")
    axes[1].set_title("Quality Factor")
    if rvals:
        axes[2].plot(x, rvals, color="#2ca02c", lw=2)
    axes[2].set_ylabel("Rse (ohm)")
    axes[2].set_xlabel("Frequency (GHz)")
    axes[2].set_title("Series Resistance")
    summary = data.get("summary", {})
    max_q = summary.get("max_Q") if isinstance(summary, dict) else None
    if max_q:
        mx = max_q["f_Hz"] / 1e9
        my = max_q["Q"]
        axes[1].scatter([mx], [my], color="#d62728", s=45, zorder=3)
        axes[1].annotate(
            f"max Q={my:.2f}\n@ {mx:.2f} GHz",
            xy=(mx, my),
            xytext=(mx + 5, my - 2),
            arrowprops={"arrowstyle": "->", "color": "#555"},
            fontsize=9,
        )
    for ax in axes:
        ax.set_xlim(min(x), max(x))
        ax.grid(True, alpha=0.32)
    fig.tight_layout(rect=[0.05, 0.04, 0.98, 0.92])
    fig.savefig(png_path, dpi=180)
    return png_path


def cmd_status(args: argparse.Namespace) -> int:
    bridge = PeakViewBridge(args.bridge_root)
    result = bridge.skill("list(getShellEnvVar(\"DISPLAY\") getShellEnvVar(\"PEAKHOME\") getWorkingDir())", timeout=20)
    print(result.output)
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    bridge = PeakViewBridge(args.bridge_root)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _safe_tag(args.cell)
    local_json = out_dir / f"{tag}.json"
    remote_json = f"/tmp/{tag}_pkv_tool_results.json"
    lines = peakdesign_script_for_design(args, remote_json)
    marker, log, result_path = bridge.run_peakdesign_script(
        f"{tag}_pkv_tool",
        lines,
        project=args.project,
        peakdesign=args.peakdesign,
        timeout=args.timeout,
        poll=args.poll,
    )
    result_text = bridge.read_remote_text(result_path, timeout=max(120, args.timeout))
    if result_text.startswith('"') and result_text.endswith('"'):
        result_text = ast.literal_eval(result_text)
    local_json.write_text(result_text, encoding="utf-8")
    data = json.loads(result_text)

    if args.sync and data.get("skill_path"):
        bridge.load_skill(data["skill_path"])
        data["cadence_verify"] = bridge.verify_cell_views(data["lib"], data["cell"])
        local_json.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        if args.open_layout:
            data["opened_window"] = bridge.open_layout(data["lib"], data["cell"])
            local_json.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    png = make_plot(local_json) if args.plot else None
    print(json.dumps({"result": str(local_json), "plot": str(png) if png else None, "cell": data.get("cell"), "summary": data.get("summary"), "skill_path": data.get("skill_path")}, indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    bridge = PeakViewBridge(args.bridge_root)
    remote_json = f"/tmp/{_safe_tag(args.cell)}_sync_results.json"
    lines = [
        "showGUI(False)",
        "import json",
        f"project=openProject(dir={args.project!r}, profile={args.profile!r})",
        f"skill_path=project.genSkillCode(cell={args.cell!r}, lib={args.lib!r})",
        f"open({remote_json!r},'w').write(json.dumps({{'cell':{args.cell!r},'lib':{args.lib!r},'skill_path':skill_path}}, indent=2))",
        "f=open('/tmp/%s_sync.marker','w'); f.write('json_written\\n'); f.close()" % _safe_tag(args.cell),
        "project.close(exitApp=True)",
        "exit(keepGUI=False)",
    ]
    marker, log, result_path = bridge.run_peakdesign_script(
        f"{_safe_tag(args.cell)}_sync",
        lines,
        project=args.project,
        peakdesign=args.peakdesign,
        timeout=args.timeout,
        poll=args.poll,
    )
    text = bridge.read_remote_text(result_path)
    if text.startswith('"') and text.endswith('"'):
        text = ast.literal_eval(text)
    data = json.loads(text)
    if args.load:
        bridge.load_skill(data["skill_path"])
        data["cadence_verify"] = bridge.verify_cell_views(args.lib, args.cell)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def _recipe_with_defaults(recipe: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    defaults = {
        "project": args.project,
        "profile": args.profile,
        "lib": args.lib,
        "open": True,
    }
    return _deep_merge(defaults, recipe)


def cmd_api(args: argparse.Namespace) -> int:
    bridge = PeakViewBridge(args.bridge_root)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    recipe = json.loads(args.recipe.read_text(encoding="utf-8"))
    recipe = _recipe_with_defaults(recipe, args)
    tag = _safe_tag(args.tag or recipe.get("name") or args.recipe.stem)
    local_json = args.output or (out_dir / f"{tag}.json")
    remote_json = f"/tmp/{tag}_results.json"
    marker_path = f"/tmp/{tag}.marker"
    lines = peakdesign_script_for_api(recipe, remote_json, marker_path)
    marker, log, result_path = bridge.run_peakdesign_script(
        tag,
        lines,
        project=recipe["project"],
        peakdesign=args.peakdesign,
        timeout=args.timeout,
        poll=args.poll,
    )
    result_text = bridge.read_remote_text(result_path, timeout=max(120, args.timeout))
    if result_text.startswith('"') and result_text.endswith('"'):
        result_text = ast.literal_eval(result_text)
    local_json.write_text(result_text, encoding="utf-8")
    data = json.loads(result_text)
    failed = [step for step in data.get("steps", []) if not step.get("ok")]
    print(json.dumps({"ok": data.get("ok"), "result": str(local_json), "steps": len(data.get("steps", [])), "failed": failed}, indent=2))
    return 0 if data.get("ok") else 1


def cmd_api_selftest(args: argparse.Namespace) -> int:
    bridge = PeakViewBridge(args.bridge_root)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    recipe = default_api_smoke_recipe(args.cell_prefix)
    recipe = _recipe_with_defaults(recipe, args)
    tag = _safe_tag(recipe["name"])
    local_json = args.output or (out_dir / f"{tag}.json")
    remote_json = f"/tmp/{tag}_results.json"
    marker_path = f"/tmp/{tag}.marker"
    lines = peakdesign_script_for_api(recipe, remote_json, marker_path)
    marker, log, result_path = bridge.run_peakdesign_script(
        tag,
        lines,
        project=recipe["project"],
        peakdesign=args.peakdesign,
        timeout=args.timeout,
        poll=args.poll,
    )
    result_text = bridge.read_remote_text(result_path)
    if result_text.startswith('"') and result_text.endswith('"'):
        result_text = ast.literal_eval(result_text)
    local_json.write_text(result_text, encoding="utf-8")
    data = json.loads(result_text)
    failed = [step for step in data.get("steps", []) if not step.get("ok")]
    cleanup_failed = [step for step in data.get("cleanup", []) if not step.get("ok")]
    print(
        json.dumps(
            {
                "ok": data.get("ok") and not failed,
                "result": str(local_json),
                "steps": len(data.get("steps", [])),
                "failed": failed,
                "cleanup_failed": cleanup_failed,
            },
            indent=2,
        )
    )
    return 0 if data.get("ok") and not failed else 1


def _normalize_catalog(data: dict[str, Any]) -> dict[str, Any]:
    """Keep older generated catalogs compatible with newer lookup modes."""
    if data.get("registered_models_with_plem"):
        return data
    registered = data.get("registered_models", [])
    with_plem = []
    for item in registered:
        normal = copy.deepcopy(item)
        normal.setdefault("is_plem_variant", False)
        with_plem.append(normal)
        plem = copy.deepcopy(item)
        plem["name"] = f"{item['name']}___PLEM__"
        plem["is_plem_variant"] = True
        with_plem.append(plem)
    data["registered_models_with_plem"] = sorted(with_plem, key=lambda item: item["name"])
    return data


def _load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"PCircuit catalog not found: {path}. Run `models --refresh` first.")
    return _normalize_catalog(json.loads(path.read_text(encoding="utf-8")))


def _refresh_catalog(args: argparse.Namespace) -> dict[str, Any]:
    bridge = PeakViewBridge(args.bridge_root)
    tag = "peakview_pcircuit_catalog"
    remote_json = f"/tmp/{tag}_results.json"
    marker_path = f"/tmp/{tag}.marker"
    lines = peakdesign_script_for_models(args, remote_json, marker_path)
    marker, log, result_path = bridge.run_peakdesign_script(
        tag,
        lines,
        project=args.project,
        peakdesign=args.peakdesign,
        timeout=args.timeout,
        poll=args.poll,
    )
    result_text = bridge.read_remote_text(result_path)
    if result_text.startswith('"') and result_text.endswith('"'):
        result_text = ast.literal_eval(result_text)
    data = _normalize_catalog(json.loads(result_text))
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Unknown PCircuit catalog refresh error"))
    args.catalog.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def _model_matches(item: dict[str, Any], query: str) -> bool:
    haystack = json.dumps(item, sort_keys=True, default=str).lower()
    return query.lower() in haystack


def _print_models_table(rows: list[dict[str, Any]], *, all_models: bool) -> None:
    if not rows:
        print("No matching PCircuit models.")
        return
    if all_models:
        headers = ("Name", "Params", "Dyn")
        body = [(r["name"], str(r.get("parameter_count", "")), "yes" if r.get("is_dynamic") else "") for r in rows]
    else:
        headers = ("Display Name", "Internal Name", "Params", "Dyn")
        body = [
            (
                r["display_name"],
                r["internal_name"],
                str(r.get("parameter_count", "")),
                "yes" if r.get("is_dynamic") else "",
            )
            for r in rows
        ]
    widths = [len(h) for h in headers]
    for row in body:
        widths = [max(widths[i], len(row[i])) for i in range(len(widths))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in body:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def cmd_models(args: argparse.Namespace) -> int:
    if args.refresh:
        data = _refresh_catalog(args)
    else:
        data = _load_catalog(args.catalog)

    if args.plem:
        rows = data.get("registered_models_with_plem") or data["registered_models"]
    elif args.all:
        rows = data["registered_models"]
    else:
        rows = data["display_models"]
    if args.dynamic:
        rows = [r for r in rows if r.get("is_dynamic")]
    if args.search:
        rows = [r for r in rows if _model_matches(r, args.search)]

    if args.json:
        print(json.dumps({"counts": data.get("counts"), "models": rows}, indent=2, sort_keys=True))
    else:
        counts = data.get("counts", {})
        print(
            "PCircuit catalog: "
            f"{counts.get('display_models')} display models, "
            f"{counts.get('registered_models')} registered models "
            f"({counts.get('registered_with_plem')} including PLEM variants), "
            f"{counts.get('dynamic_models')} dynamic models"
        )
        print(f"Catalog: {args.catalog}")
        _print_models_table(rows, all_models=args.all or args.plem)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PeakView/PeakDesign automation via virtuoso-bridge")
    parser.add_argument("--bridge-root", type=Path, default=DEFAULT_BRIDGE_ROOT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=DEFAULT_PROJECT)
    common.add_argument("--profile", default=DEFAULT_PROFILE)
    common.add_argument("--lib", default=DEFAULT_LIB)
    common.add_argument("--peakdesign", default=DEFAULT_PEAKDESIGN)
    common.add_argument("--timeout", type=int, default=2400)
    common.add_argument("--poll", type=int, default=10)

    status = sub.add_parser("status", parents=[common], help="Check bridge-visible PeakView environment")
    status.set_defaults(func=cmd_status)

    design = sub.add_parser("design", parents=[common], help="Create, optionally simulate, plot, and sync a PeakView PCircuit")
    design.add_argument("--pcircuit", required=True, help="PeakView PCircuit name, e.g. Octagon or AdvOctSymmetric")
    design.add_argument("--cell", required=True)
    design.add_argument("--itype", default="PL")
    design.add_argument("--freq-start", default="1.000e+09")
    design.add_argument("--freq-stop", default="1.000e+11")
    design.add_argument("--points", type=int, default=50)
    design.add_argument("--sweep-accuracy", default="RFIC high")
    design.add_argument("--scale", default="lin", choices=["lin", "logSweep"])
    design.add_argument("--simulate", action="store_true")
    design.add_argument("--sim-timeout", type=int, default=1800)
    design.add_argument("--generate-sync", action="store_true", help="Generate Cadence sync SKILL but do not load it")
    design.add_argument("--sync", action="store_true", help="Generate and load Cadence sync SKILL")
    design.add_argument("--open-layout", action="store_true")
    design.add_argument("--replace", action="store_true", help="Delete existing PeakView cell with the same name before creating")
    design.add_argument("--formula", action="append", help="Extra PeakDesign formula to evaluate")
    design.add_argument("--plot", action="store_true")
    design.add_argument("--output-dir", type=Path, default=Path.cwd())
    design.set_defaults(func=cmd_design)

    sync = sub.add_parser("sync", parents=[common], help="Generate/load Cadence sync SKILL for an existing PeakView cell")
    sync.add_argument("--cell", required=True)
    sync.add_argument("--load", action="store_true", help="Load generated SKILL into Virtuoso")
    sync.set_defaults(func=cmd_sync)

    api = sub.add_parser("api", parents=[common], help="Run a JSON PeakDesign Script API recipe")
    api.add_argument("--recipe", type=Path, required=True)
    api.add_argument("--tag", help="Remote job/result tag. Defaults to recipe name or file stem")
    api.add_argument("--output", type=Path)
    api.add_argument("--output-dir", type=Path, default=Path.cwd())
    api.set_defaults(func=cmd_api)

    api_selftest = sub.add_parser("api-selftest", parents=[common], help="Verify the generic Script API recipe runner")
    api_selftest.add_argument("--cell-prefix", default="Codex_API_Smoke")
    api_selftest.add_argument("--output", type=Path)
    api_selftest.add_argument("--output-dir", type=Path, default=Path.cwd())
    api_selftest.set_defaults(func=cmd_api_selftest)

    models = sub.add_parser("models", parents=[common], help="List/search PeakView PCircuit models")
    models.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    models.add_argument("--refresh", action="store_true", help="Read the live PeakDesign PCircuit registry and update the local catalog")
    models.add_argument("--search", help="Case-insensitive search across names and parameters")
    models.add_argument("--all", action="store_true", help="Show all registered internal model names instead of GUI display names")
    models.add_argument("--plem", action="store_true", help="Show all registered internal model names including PLEM variants")
    models.add_argument("--dynamic", action="store_true", help="Only show dynamic PCircuits")
    models.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    models.set_defaults(func=cmd_models)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
