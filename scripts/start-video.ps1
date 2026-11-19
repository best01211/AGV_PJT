param(
    [string]$CameraUrl = 'http://70.12.231.81:8765/video',
    [ValidateSet('mjpeg', 'snapshot')][string]$CameraFormat = 'mjpeg',
    [int]$Port = 8000
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    throw 'Project virtual environment is missing. Install requirements first.'
}
$env:JETANK_MODE = 'mock'
$env:JETANK_CAMERA_URL = $CameraUrl
$env:JETANK_CAMERA_FORMAT = $CameraFormat
& $ProjectPython -m uvicorn backend.main:app --app-dir $ProjectRoot --host 127.0.0.1 --port $Port --no-access-log
