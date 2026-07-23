@echo off
REM Run on Windows desktop after: git pull && scripts\build_native.bat
setlocal
cd /d "%~dp0\.."
set PATH=%LOCALAPPDATA%\Programs\GNU Octave\Octave-11.3.0\mingw64\bin;%PATH%
set PYTHONPATH=native\build;src
set OMP_NUM_THREADS=%NUMBER_OF_PROCESSORS%
set PYTHONUNBUFFERED=1

echo === native info ===
.venv\Scripts\python.exe -c "from physics_router.native_bridge import info; import json; print(json.dumps(info(), indent=2))"
if errorlevel 1 exit /b 1

echo === unit tests ===
.venv\Scripts\pytest.exe -q tests\test_native_core.py tests\test_golden_eval.py tests\test_route_diagnostics.py tests\test_graph_theory.py --tb=line
if errorlevel 1 exit /b 1

echo === simple_2net golden ===
.venv\Scripts\python.exe -u -c "from pathlib import Path; import json,time,os; from physics_router.golden_eval import evaluate_board; e={'id':'simple_2net','pcb':str(Path('tests/fixtures/golden/simple_2net.kicad_pcb').resolve()),'expect':'manufacturing_gate','timeout_s':0,'min_completion':1.0,'hard_deadline':False,'cbs_repair':False,'_base':str(Path('.').resolve())}; t=time.time(); r=evaluate_board(e,pipeline='capacity',effort=0.45,out_dir=Path('viewer/runs/simple_2net_win'),hard_deadline=False,cbs_repair=False); print('grade',r.get('golden_grade'),'comp',r.get('completion_ratio'),'drc',r.get('hard_violations'),'t',round(time.time()-t,3)); json.dump(r,open('viewer/runs/simple_2net_win/row.json','w'),indent=2,default=str)"

echo === mppc flagship (long) ===
.venv\Scripts\python.exe -u -c "from pathlib import Path; import json,time; from physics_router.golden_eval import evaluate_board; root=Path('.').resolve(); e={'id':'mppc_v1.3','pcb':str(root/'examples/mppc-interface/mppcInterface_v1.3.kicad_pcb'),'config':str(root/'examples/mppc-interface/placement_config.yaml'),'expect':'partial_ok','timeout_s':0,'min_completion':0.0,'hard_deadline':False,'cbs_repair':False,'_base':str(root)}; out=root/'viewer/runs/mppc_v1.3_win'; out.mkdir(parents=True,exist_ok=True); print('routing mppc...'); t=time.time(); r=evaluate_board(e,pipeline='capacity',effort=0.55,out_dir=out,hard_deadline=False,cbs_repair=False); r['benchmark_wall_s']=round(time.time()-t,2); print('elapsed',r['benchmark_wall_s'],'grade',r.get('golden_grade'),'score',r.get('golden_score'),'comp',r.get('completion_ratio'),'drc',r.get('hard_violations')); json.dump(r,open(out/'benchmark_row.json','w'),indent=2,default=str); print('wrote',out/'benchmark_row.json')"

echo done
endlocal
