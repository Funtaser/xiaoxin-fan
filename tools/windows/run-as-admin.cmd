@echo off
rem Double-click to run fan-probe.ps1 as Administrator
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%~dp0fan-probe.ps1'"
