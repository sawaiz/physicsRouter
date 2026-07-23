@echo off
REM Build pr_native on Windows (MinGW from Octave or MSYS2 + optional Ninja)
setlocal EnableDelayedExpansion
cd /d "%~dp0\.."

set BUILD_DIR=native\build
set JOBS=%NUMBER_OF_PROCESSORS%
if "%JOBS%"=="" set JOBS=4
set OMP_NUM_THREADS=%JOBS%

REM Prefer Octave MinGW if present
set MINGW=
if exist "%LOCALAPPDATA%\Programs\GNU Octave\Octave-11.3.0\mingw64\bin\g++.exe" (
  set "MINGW=%LOCALAPPDATA%\Programs\GNU Octave\Octave-11.3.0\mingw64\bin"
)
if exist "%LOCALAPPDATA%\Programs\GNU Octave\Octave-10.2.0\mingw64\bin\g++.exe" (
  set "MINGW=%LOCALAPPDATA%\Programs\GNU Octave\Octave-10.2.0\mingw64\bin"
)
if not "%MINGW%"=="" set "PATH=%MINGW%;%PATH%"

where ninja >nul 2>nul
if errorlevel 1 (
  echo Install ninja: pip install ninja
  exit /b 1
)

cmake -S native -B %BUILD_DIR% -G Ninja ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_CXX_COMPILER=g++.exe ^
  -DPR_WITH_OPENMP=ON ^
  -DPR_WITH_OPENCL=ON ^
  -DPR_BUILD_PYTHON=ON ^
  -DPR_BUILD_CLI=ON
if errorlevel 1 exit /b 1
cmake --build %BUILD_DIR% -j %JOBS%
if errorlevel 1 exit /b 1

REM MinGW runtime next to the extension for DLL load
if not "%MINGW%"=="" (
  copy /Y "%MINGW%\libgcc_s_seh-1.dll" "%BUILD_DIR%\" >nul
  copy /Y "%MINGW%\libstdc++-6.dll" "%BUILD_DIR%\" >nul
  copy /Y "%MINGW%\libwinpthread-1.dll" "%BUILD_DIR%\" >nul 2>nul
  copy /Y "%MINGW%\libgomp-1.dll" "%BUILD_DIR%\" >nul 2>nul
)
REM OpenCL ICD loader is System32\OpenCL.dll (NVIDIA driver) — already on PATH

echo Built %BUILD_DIR% with OMP_NUM_THREADS=%OMP_NUM_THREADS%
dir %BUILD_DIR%\pr_native*
endlocal
