#!/bin/sh
# Build the PES2012 player runtime (32-bit Windows DLLs). Run from this dir.
# Needs: i686-w64-mingw32-g++ (e.g. apt install g++-mingw-w64-i686)
set -e
CXX="${CXX:-i686-w64-mingw32-g++}"
$CXX -O2 -shared -static -o drawlogic.dll drawlogic.cpp -ld3d9
$CXX -O2 -shared -static -o drawhook.dll drawhook.cpp -lwinmm
ls -la drawlogic.dll drawhook.dll
