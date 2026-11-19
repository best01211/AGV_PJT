param(
    [string]$CameraUrl = 'http://70.12.231.81:8766/frame.jpg',
    [int]$Port = 8001
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:JETANK_CAMERA_CALIBRATION = Join-Path $ProjectRoot 'config/c270_calibration.json'
$env:JETANK_C270_CAPTURE_DIR = Join-Path $ProjectRoot 'tmp/c270-calibration-captures'
$env:JETANK_DEPTH_PREVIEW = '1'
$env:JETANK_DEPTH_ENGINE = 'depthpro'
$env:JETANK_DEPTH_MEASUREMENT_DIR = Join-Path $ProjectRoot 'data/depth_evaluation_c270'
# Reproduce the 60/80 cm wall experiment at the original 640x480 focal estimate.
$env:JETANK_C270_PREVIEW_FOCAL_PX = '609.0258178710938'
& (Join-Path $PSScriptRoot 'start-video.ps1') -CameraUrl $CameraUrl -CameraFormat snapshot -Port $Port
