$ErrorActionPreference = "Stop"

$VenvPython = ".\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    py -3 -m venv .venv
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e . pyinstaller

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "Snagger",
    "--paths", "src",
    "--collect-all", "yt_dlp",
    "--collect-data", "imageio_ffmpeg",
    "run_app.py"
)

& $VenvPython -m PyInstaller @PyInstallerArgs

Write-Host ""
Write-Host "Built: dist\Snagger.exe"
