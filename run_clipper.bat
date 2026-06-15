@echo off
cd /d "C:\Users\lenovo-audit\test_pipeline"
call venv\Scripts\activate.bat
python content_factory.py >> logs\clipper_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log 2>&1