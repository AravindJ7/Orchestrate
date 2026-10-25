@echo off
cd /d "%~dp0"
echo Starting Ghost Data Still Exists UI...
echo.
echo Installing dependencies if needed...
pip install -q -r requirements.txt
echo.
echo Starting Flask app...
echo Open http://localhost:5000 in your browser
echo.
python app.py
pause
